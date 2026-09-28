"""The runnable examples and the documented quick start keep working (Task 13).

Each ``examples/<name>.py`` exposes ``main() -> int`` and runs offline on
``FakeProvider``; the quick-start block in ``docs/python-sdk.md`` is executed with a
``FakeProvider`` registered for every model alias, so the docs cannot rot silently.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

import moeka
from moeka.testing import FakeProvider

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"
SDK_DOC = ROOT / "docs" / "python-sdk.md"


def _load(name: str) -> ModuleType:
    path = EXAMPLES / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"moeka_example_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered first, as a normal import would be: pydantic resolves the models'
    # postponed annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", ["batch_json", "coach_sim"])
def test_example_main_succeeds(name: str, capfd: pytest.CaptureFixture[str]) -> None:
    assert _load(name).main() == 0
    assert capfd.readouterr().out.strip()


def test_every_example_is_tested() -> None:
    found = {p.stem for p in EXAMPLES.glob("*.py")}
    assert found == {"batch_json", "coach_sim"}


def _quickstart() -> str:
    text = SDK_DOC.read_text(encoding="utf-8")
    block = re.search(
        r"<!-- quickstart:begin[^>]*-->\s*```python\n(.*?)```\s*<!-- quickstart:end -->",
        text, re.DOTALL,
    )
    assert block is not None, "quick-start markers missing from docs/python-sdk.md"
    return block.group(1)


def test_quickstart_snippet_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str],
) -> None:
    fake = FakeProvider(default='{"label": "bug", "confidence": 0.9}')

    class OfflineKernel(moeka.Kernel):
        """The documented Kernel, with the fake serving every model alias."""

        def __init__(self, env: moeka.Environment, **kwargs: object) -> None:
            super().__init__(env, **kwargs)  # type: ignore[arg-type]
            for alias, spec in env.models.items():
                self.llm.register_provider(alias, fake, spec)

    monkeypatch.setattr(moeka, "Kernel", OfflineKernel)
    monkeypatch.chdir(tmp_path)  # the snippet's relative state/work dirs land here
    exec(compile(_quickstart(), str(SDK_DOC), "exec"), {"__name__": "quickstart"})
    out = capfd.readouterr().out
    assert "label='bug'" in out and "confidence=0.9" in out
    assert len(fake.calls) == 1
    assert (tmp_path / "moeka-state").is_dir()
