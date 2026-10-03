"""Part B: protected trust paths (FR-008): the file tools never write inside a configured trust
dir, nor any file named ``CERT.json`` or ``revoked.jsonl``. Driven through a kernel agent's real
file tools (FakeProvider tool calls)."""

from __future__ import annotations

from pathlib import Path

import pytest
from _h012 import (
    agent_of,
    caps_for,
    fake_with,
    tool_call,
    tool_messages,
)


def _setup(tmp_path):
    work = tmp_path / "work"
    (work / "skills" / "h012-p").mkdir(parents=True)
    (work / "skills" / "h012-p" / "SKILL.md").write_text(
        "---\nname: h012-p\ndescription: protected fixture\n---\n\nbody\n", encoding="utf-8")
    t_in = work / "trust-in"
    t_out = tmp_path / "trust-out"
    t_in.mkdir()
    t_out.mkdir()
    return work, t_in, t_out, caps_for(False, t_in, t_out)


def _run(kmaker, work, caps, *calls):
    fake = fake_with(*[tool_call(n, a, f"c{i}") for i, (n, a) in enumerate(calls)], "done")
    kernel, fake = kmaker(work=work, caps=caps, fake=fake)
    agent_of(kernel).run_sync("go")
    return tool_messages(fake)


TARGETS = {
    "trust-dir-in-workspace": lambda w, ti, to, r: ti / "new.pub",
    "trust-dir-outside-workspace": lambda w, ti, to, r: to / "notes.txt",
    "trust-dir-subdir": lambda w, ti, to, r: ti / "sub" / "deep.txt",
    "cert-in-bundle": lambda w, ti, to, r: w / "skills" / "h012-p" / "CERT.json",
    "cert-anywhere": lambda w, ti, to, r: w / "notes" / "CERT.json",
    "cert-outside-workspace": lambda w, ti, to, r: r / "elsewhere" / "CERT.json",
    "revoked-in-workspace": lambda w, ti, to, r: w / "revoked.jsonl",
    "revoked-in-trust-dir": lambda w, ti, to, r: ti / "revoked.jsonl",
    "revoked-outside-workspace": lambda w, ti, to, r: r / "elsewhere" / "revoked.jsonl",
}


@pytest.mark.fr("FR-008")
@pytest.mark.parametrize("target", sorted(TARGETS))
def test_write_file_denied(kmaker, tmp_path, target):
    work, t_in, t_out, caps = _setup(tmp_path)
    path = TARGETS[target](work, t_in, t_out, tmp_path)
    msgs = _run(kmaker, work, caps, ("write_file", {"path": str(path), "content": "PWNED"}))
    assert not path.exists(), f"{path} was written"
    assert msgs and "error" in msgs[-1].lower()


@pytest.mark.fr("FR-008")
def test_relative_path_into_trust_dir_denied(kmaker, tmp_path):
    work, t_in, _t_out, caps = _setup(tmp_path)
    _run(kmaker, work, caps, ("write_file", {"path": "trust-in/rel.pub", "content": "PWNED"}))
    assert not (t_in / "rel.pub").exists()


@pytest.mark.fr("FR-008")
@pytest.mark.parametrize("name", ["CERT.json", "revoked.jsonl"])
def test_overwrite_existing_denied(kmaker, tmp_path, name):
    work, t_in, _t_out, caps = _setup(tmp_path)
    path = (work / "skills" / "h012-p" / name) if name == "CERT.json" else (t_in / name)
    path.write_text("ORIGINAL\n", encoding="utf-8")
    _run(kmaker, work, caps, ("write_file", {"path": str(path), "content": "PWNED"}))
    assert path.read_text(encoding="utf-8") == "ORIGINAL\n"


@pytest.mark.fr("FR-008")
@pytest.mark.parametrize("name", ["CERT.json", "revoked.jsonl", "trusted.pub"])
def test_edit_existing_denied(kmaker, tmp_path, name):
    work, t_in, _t_out, caps = _setup(tmp_path)
    path = (work / "skills" / "h012-p" / name) if name == "CERT.json" else (t_in / name)
    path.write_text("ORIGINAL line\n", encoding="utf-8")
    _run(kmaker, work, caps,
         ("read_file", {"path": str(path)}),
         ("edit_file", {"path": str(path), "old_text": "ORIGINAL", "new_text": "PWNED"}))
    assert path.read_text(encoding="utf-8") == "ORIGINAL line\n"


@pytest.mark.fr("FR-008")
def test_symlink_into_trust_dir_denied(kmaker, tmp_path):
    work, _t_in, t_out, caps = _setup(tmp_path)
    (work / "door").symlink_to(t_out, target_is_directory=True)
    _run(kmaker, work, caps, ("write_file", {"path": str(work / "door" / "x.pub"),
                                             "content": "PWNED"}))
    assert not (t_out / "x.pub").exists()


# -- controls: the denial is attributable to FR-008 -------------------------------------------------


@pytest.mark.fr("FR-008")
@pytest.mark.parametrize("where", ["workspace", "bundle-other-file", "outside-workspace"])
def test_ordinary_writes_still_allowed(kmaker, tmp_path, where):
    work, _t_in, _t_out, caps = _setup(tmp_path)
    path = {
        "workspace": work / "notes.txt",
        "bundle-other-file": work / "skills" / "h012-p" / "OTHER.json",
        "outside-workspace": tmp_path / "elsewhere" / "plain.txt",
    }[where]
    _run(kmaker, work, caps, ("write_file", {"path": str(path), "content": "fine"}))
    assert path.read_text(encoding="utf-8") == "fine"


@pytest.mark.fr("FR-008")
def test_reading_cert_json_is_allowed(kmaker, tmp_path):
    work, t_in, _t_out, caps = _setup(tmp_path)
    cert = work / "skills" / "h012-p" / "CERT.json"
    cert.write_text('{"READ-ME-MARK": 1}', encoding="utf-8")
    (t_in / "k.pub").write_text("KEY-READ-MARK\n", encoding="utf-8")
    msgs = _run(kmaker, work, caps, ("read_file", {"path": str(cert)}),
                ("read_file", {"path": str(t_in / "k.pub")}))
    assert any("READ-ME-MARK" in m for m in msgs)
    assert any("KEY-READ-MARK" in m for m in msgs)


@pytest.mark.fr("FR-008")
def test_unconfigured_dir_is_not_a_trust_dir(kmaker, tmp_path):
    work, _t_in, _t_out, _caps = _setup(tmp_path)
    path = work / "trust-in" / "plain.txt"
    _run(kmaker, work, None, ("write_file", {"path": str(path), "content": "fine"}))
    assert Path(path).read_text(encoding="utf-8") == "fine"
