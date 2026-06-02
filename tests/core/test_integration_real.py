"""Real end-to-end test of the moeka-core embedding path.

Exercises the whole chain the way a host would: secrets come from the
environment (the keys.env pattern), an in-memory ``config_dict`` references them
via ``${VAR}``, and a registered Python action is actually invoked by a live LLM.

Skips automatically when no provider key is available, so CI without secrets
stays green. Run locally with a populated repo-root ``keys.env`` (gitignored) or
an exported ``OPENROUTER_API_KEY``.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_keys_env() -> None:
    """Populate os.environ from the gitignored repo-root keys.env (best effort).

    Mirrors what moeka.sh does at launch: KEY=VALUE lines are exported into the
    environment so ``${VAR}`` config placeholders resolve. Existing environment
    values win (never override an already-set var).
    """
    keys = _REPO_ROOT / "keys.env"
    if not keys.exists():
        return
    for line in keys.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip().strip('"').strip("'")
        if name and value and name not in os.environ:
            os.environ[name] = value


_load_keys_env()

_HAS_KEY = bool(os.environ.get("OPENROUTER_API_KEY"))
_MODEL = os.environ.get("MOEKA_TEST_MODEL", "google/gemini-3-flash-preview")

pytestmark = pytest.mark.skipif(
    not _HAS_KEY,
    reason="no OPENROUTER_API_KEY (set it or fill repo-root keys.env) — skipping live test",
)


@pytest.mark.asyncio
async def test_live_action_called_by_real_llm(tmp_path):
    """A live model must call a registered action and use its returned value.

    The action returns a freshly minted secret the model cannot guess, so the
    secret appearing in the reply proves the real runner -> ToolRegistry ->
    FunctionTool round-trip executed against a live provider.
    """
    from nanobot.core import MoekaCore

    secret = f"MOEKA-{secrets.token_hex(4).upper()}"
    config = {
        "providers": {"openrouter": {"apiKey": "${OPENROUTER_API_KEY}"}},
        "agents": {"defaults": {"model": _MODEL, "provider": "openrouter"}},
    }
    core = MoekaCore.create(config_dict=config, workspace=tmp_path)

    @core.action
    def get_launch_code(system: str) -> str:
        """Return the secret launch code for the named system."""
        return secret

    result = await core.run(
        "Call the get_launch_code tool for system 'alpha' and tell me the exact "
        "code it returns. Reply with the code verbatim."
    )

    assert "get_launch_code" in result.tools_used, result
    assert secret in result.content, f"model did not relay tool result: {result.content!r}"


@pytest.mark.asyncio
async def test_live_complete_one_shot(tmp_path):
    """The one-shot completion path works against a live provider."""
    from nanobot.core import MoekaCore

    config = {
        "providers": {"openrouter": {"apiKey": "${OPENROUTER_API_KEY}"}},
        "agents": {"defaults": {"model": _MODEL, "provider": "openrouter"}},
    }
    # complete() routes through the same config/provider layer.
    text = await MoekaCore.complete(
        "Reply with exactly the word: pong",
        config_dict=config,
    )
    assert "pong" in text.lower()
