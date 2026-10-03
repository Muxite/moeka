"""Part F: docs (FR-005): docs/PLAN.md repository facts, spec 004 status and FR-008 pin,
and the local findings register in docs/findings/README.md."""

from __future__ import annotations

import re

import pytest
from _h010 import repo_root

REPO = repo_root()
OLD_PIN = "the pin is `core-slim` `6f80c392` until consolidation (`003`) moves it to `main`"


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


# -- docs/PLAN.md --------------------------------------------------------------------------------


@pytest.mark.fr("FR-005")
def test_repo_has_dockerfile_and_compose():
    assert (REPO / "Dockerfile").is_file()
    assert (REPO / "compose.yaml").is_file()


@pytest.mark.fr("FR-005")
def test_plan_no_longer_claims_there_is_no_dockerfile():
    text = _read("docs/PLAN.md")
    flat = re.sub(r"\s+", " ", text)
    assert not re.search(r"\b(has|have|there is|there are)\s+no\s+`?Dockerfile", flat, re.I), (
        "docs/PLAN.md still says the repo has no Dockerfile")
    assert not re.search(r"\bno\s+`?Dockerfile`?\s*,\s*`?docker-compose", flat, re.I)


@pytest.mark.fr("FR-005")
def test_plan_names_the_current_dockerfile_and_compose_file():
    text = _read("docs/PLAN.md")
    assert "compose.yaml" in text, "docs/PLAN.md must name the repo's compose.yaml"
    status = text.split("\n## ", 1)[0]
    assert "Dockerfile" in status and "compose" in status.lower(), (
        "the status block at the top of docs/PLAN.md must state the Dockerfile/compose facts")


# -- specs/004-rsi-harness/spec.md ---------------------------------------------------------------


def _status_004() -> str:
    text = _read("specs/004-rsi-harness/spec.md")
    m = re.search(r"\*\*Status\*\*:(.*?)(?:\n\s*\n|\n\*\*[A-Z][a-z]+\*\*:)", text, re.S)
    assert m, "spec 004 has no **Status**: field"
    return m.group(1)


@pytest.mark.fr("FR-005")
def test_spec004_status_no_longer_says_nothing_is_built():
    status = _status_004()
    assert "Nothing in the harness is built" not in status
    assert not re.search(r"\bnothing\b[^.]*\bbuilt\b", status, re.I), status


@pytest.mark.fr("FR-005")
@pytest.mark.parametrize("term", [r"containment", r"held-?out", r"sandbox"])
def test_spec004_status_names_what_is_built(term):
    assert re.search(term, _status_004(), re.I), _status_004()


@pytest.mark.fr("FR-005")
def test_spec004_status_says_where_the_harness_lives():
    status = _status_004()
    assert "moeka-rsi" in status, status
    assert re.search(r"001\s*(-|–|—|to|through)\s*005", status), status


@pytest.mark.fr("FR-005")
def test_spec004_fr008_pin_is_corrected():
    text = _read("specs/004-rsi-harness/spec.md")
    m = re.search(r"\*\*FR-008\*\*:(.*?)(?:\n- \*\*FR-|\n\n)", text, re.S)
    assert m, "spec 004 lost FR-008"
    fr008 = re.sub(r"\s+", " ", m.group(1))
    assert OLD_PIN not in fr008, fr008
    assert re.search(r"\bpin", fr008, re.I), fr008


# -- docs/findings/README.md ---------------------------------------------------------------------

ITEMS = {
    "R8": (r"\bR8\b", r"\bCI\b"),
    "R11": (r"\bR11\b", r"overlap"),
    "CLARIFY Q1": (r"\bQ1\b(?!\d)", None),
    "Q16": (r"\bQ16\b", None),
    "usage review #13": (r"#\s?13\b", None),
}


def _register() -> str:
    return _read("docs/findings/README.md")


def _block(text: str, pattern: str) -> str:
    """The table row or list item (with its continuation lines) that mentions *pattern*."""
    lines = text.splitlines()
    item = re.compile(r"\s*(\||[-*+]\s|\d+\.\s|#{2,6}\s)")
    order = [i for i, ln in enumerate(lines) if item.match(ln)]
    order += [i for i in range(len(lines)) if i not in order]
    for i in order:
        line = lines[i]
        if re.search(pattern, line):
            block = [line]
            if re.match(r"\s*#{2,6}\s", line):  # a heading: its section, to the next heading
                for nxt in lines[i + 1:]:
                    if re.match(r"\s*#{1,6}\s", nxt):
                        break
                    block.append(nxt)
            elif not line.lstrip().startswith("|"):
                for nxt in lines[i + 1:]:
                    if not nxt.strip() or re.match(r"([-*+]\s|\d+\.\s|#|\|)", nxt):
                        break
                    block.append(nxt)
            return "\n".join(block)
    return ""


@pytest.mark.fr("FR-005")
@pytest.mark.parametrize("item", list(ITEMS))
def test_register_lists_open_item(item):
    pattern, topic = ITEMS[item]
    text = _register()
    block = _block(text, pattern)
    assert block, f"{item} is not in docs/findings/README.md"
    if topic:
        assert re.search(topic, block, re.I), (item, block)


@pytest.mark.fr("FR-005")
@pytest.mark.parametrize("item", list(ITEMS))
def test_register_item_has_status_and_owner(item):
    pattern, _ = ITEMS[item]
    text = _register()
    block = _block(text, pattern)
    assert block, item
    if block.lstrip().startswith("|"):
        header = next((ln for ln in text.splitlines()
                       if ln.lstrip().startswith("|") and re.search(r"status", ln, re.I)
                       and re.search(r"owner", ln, re.I)), None)
        assert header, "the register table needs Status and Owner columns"
        cells = [c.strip() for c in block.strip().strip("|").split("|")]
        hcells = [c.strip().lower() for c in header.strip().strip("|").split("|")]
        status_i = next(i for i, c in enumerate(hcells) if "status" in c)
        owner_i = next(i for i, c in enumerate(hcells) if "owner" in c)
        assert len(cells) > max(status_i, owner_i), (block, header)
        assert cells[status_i] and cells[owner_i], (item, block)
    else:
        assert re.search(r"status", block, re.I) and re.search(r"owner", block, re.I), (
            item, block)


@pytest.mark.fr("FR-005")
def test_register_is_local_not_only_a_pointer():
    text = _register()
    assert len(re.findall(r"\bR8\b|\bR11\b|\bQ16\b", text)) >= 3


@pytest.mark.fr("FR-005")
def test_register_records_accepted_sudo_risk():
    flat = re.sub(r"\s+", " ", _register())
    assert re.search(r"allow_?sudo`?\s*(=|:|is)?\s*`?true", flat, re.I), flat[-2000:]
    assert re.search(r"restrict_?to_?workspace`?\s*(=|:|is)?\s*`?false", flat, re.I), flat[-2000:]
    assert re.search(r"accepted\s+risk|risk\b[^.]{0,80}\baccepted", flat, re.I)
    assert re.search(r"live\s+gateway", flat, re.I)
    assert re.search(r"RSI\s+VM", flat), flat[-2000:]
    assert re.search(r"ops\s+agent", flat, re.I)
