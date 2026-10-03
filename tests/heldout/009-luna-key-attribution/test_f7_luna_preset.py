"""FR-007: docs "Model presets: gpt-6-luna" section, key matrix, and 0.60 USD pricing."""

from __future__ import annotations

import json
import re

import pytest
from _h009 import record, repo_root

SPEC_PRESET = {
    "model": "openai/gpt-6-luna", "provider": "openrouter", "tier": "fast",
    "price_in_per_mtok": 0.10, "price_out_per_mtok": 0.50, "context_window_tokens": 1000000,
}
HEADING = re.compile(r"^(#{1,6})\s*model presets:\s*gpt-6-luna\b.*$", re.I | re.M)


def _doc() -> str:
    return (repo_root() / "docs" / "configuration.md").read_text(encoding="utf-8")


def _section() -> str:
    text = _doc()
    m = HEADING.search(text)
    assert m, 'no "Model presets: gpt-6-luna" heading in docs/configuration.md'
    level = len(m.group(1))
    rest = text[m.end():]
    nxt = re.search(rf"^#{{1,{level}}}\s", rest, re.M)
    return rest[: nxt.start()] if nxt else rest


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _find_presets(node) -> list[dict]:
    out = []
    if isinstance(node, dict):
        if node.get("model") == "openai/gpt-6-luna":
            out.append({_snake(k): v for k, v in node.items()})
        for v in node.values():
            out.extend(_find_presets(v))
    elif isinstance(node, list):
        for v in node:
            out.extend(_find_presets(v))
    return out


def _doc_presets() -> list[dict]:
    found = []
    for block in re.findall(r"```[a-zA-Z0-9_-]*\n(.*?)```", _section(), re.S):
        try:
            found.extend(_find_presets(json.loads(block)))
        except ValueError:
            continue
    if not found:  # an inline JSON object outside a fenced block
        for m in re.finditer(r"\{[^{}]*openai/gpt-6-luna[^{}]*\}", _section()):
            try:
                found.extend(_find_presets(json.loads(m.group(0))))
            except ValueError:
                continue
    return found


def _price_through_ledger(preset: dict, tmp_path) -> float | None:
    from nanobot.config.schema import Config
    from nanobot.kernel.ledger import LedgerObserver
    from nanobot.llm_usage import query
    from nanobot.llm_usage.store import LLMUsageStore
    from nanobot.providers.base import LLMUsage
    from nanobot.providers.factory import _ledger_pricing

    config = Config.model_validate({
        "model_presets": {"luna": preset},
        "agents": {"defaults": {"model_preset": "luna"}},
    })
    resolved = config.model_presets["luna"]
    table = _ledger_pricing(config, resolved, [])
    store = LLMUsageStore(tmp_path / "data" / "llm_usage.sqlite3")
    try:
        obs = LedgerObserver(store=store, pricing=table)
        rec = record(provider="openrouter", model="openai/gpt-6-luna",
                     usage=LLMUsage.reported(input_tokens=1_000_000, output_tokens=1_000_000))
        enriched = obs.enrich(rec)
        obs(rec)
        stored = query.totals(store)[0].cost_usd
        assert enriched.tier == "fast"
        assert stored == pytest.approx(enriched.cost_usd, abs=1e-12)
        return enriched.cost_usd
    finally:
        store.close()


@pytest.mark.fr("FR-007")
def test_section_exists():
    sec = _section()
    assert "openai/gpt-6-luna" in sec
    assert "openrouter" in sec.lower()


@pytest.mark.fr("FR-007")
def test_section_preset_values_match_spec():
    presets = _doc_presets()
    assert presets, "no parseable gpt-6-luna preset JSON in the section"
    p = presets[0]
    assert p["provider"] == "openrouter"
    assert p["tier"] == "fast"
    assert float(p["price_in_per_mtok"]) == pytest.approx(0.10, abs=1e-12)
    assert float(p["price_out_per_mtok"]) == pytest.approx(0.50, abs=1e-12)
    assert int(p["context_window_tokens"]) == 1_000_000


@pytest.mark.fr("FR-007")
def test_section_preset_loads_through_config_and_prices_060(tmp_path):
    presets = _doc_presets()
    assert presets, "no parseable gpt-6-luna preset JSON in the section"
    cost = _price_through_ledger(presets[0], tmp_path)
    assert cost == pytest.approx(0.60, abs=1e-12)


@pytest.mark.fr("FR-007")
def test_spec_preset_prices_060_through_ledger(tmp_path):
    """The spec's literal preset through Config/_ledger_pricing/LedgerObserver: 0.60 USD."""
    assert _price_through_ledger(dict(SPEC_PRESET), tmp_path) == pytest.approx(0.60, abs=1e-12)


@pytest.mark.fr("FR-007")
def test_spec_preset_camelcase_form_loads(tmp_path):
    from nanobot.config.schema import Config

    cfg = Config.model_validate({"modelPresets": {"luna": {
        "model": "openai/gpt-6-luna", "provider": "openrouter", "tier": "fast",
        "priceInPerMtok": 0.10, "priceOutPerMtok": 0.50, "contextWindowTokens": 1000000,
    }}})
    p = cfg.model_presets["luna"]
    assert (p.model, p.provider, p.tier) == ("openai/gpt-6-luna", "openrouter", "fast")
    assert p.context_window_tokens == 1_000_000


def _table_rows(sec: str) -> list[str]:
    return [ln.lower() for ln in sec.splitlines()
            if ln.strip().startswith("|") and not set(ln.strip()) <= set("|-: ")]


@pytest.mark.fr("FR-007")
def test_key_matrix_moeka_rows():
    rows = _table_rows(_section())
    assert rows, "no table in the section"
    moeka = [r for r in rows if "~/projects/moeka/keys.env" in r and "openrouter_api_key" in r]
    assert moeka, "no row mapping to ~/projects/moeka/keys.env OPENROUTER_API_KEY"
    joined = " ".join(moeka)
    assert "gateway" in joined
    assert "development" in joined or "dev" in joined
    assert not any("awork/keys.env" in r for r in moeka)


@pytest.mark.fr("FR-007")
def test_key_matrix_awork_row():
    rows = _table_rows(_section())
    awork = [r for r in rows if "~/projects/awork/keys.env" in r and "openrouter_api_key" in r]
    assert awork, "no row mapping to ~/projects/awork/keys.env OPENROUTER_API_KEY"
    assert any("awork" in r.split("~/projects/awork/keys.env")[0] for r in awork)
    assert not any("moeka/keys.env" in r for r in awork)


@pytest.mark.fr("FR-007")
def test_key_matrix_has_header_columns():
    rows = _table_rows(_section())
    header = next((r for r in rows if "variable" in r or "key file" in r), None)
    assert header is not None
    assert "consumer" in header or "who" in header or "use" in header


@pytest.mark.fr("FR-007", "FR-006")
def test_docs_contain_no_key_material():
    sec = _section()
    assert not re.search(r"sk-or-v1-[0-9a-f]{16,}", sec)
    assert not re.search(r"sk-[A-Za-z0-9_-]{32,}", sec)
