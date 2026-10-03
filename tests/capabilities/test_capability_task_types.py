"""Deterministic task typing (spec 011 FR-005)."""

from __future__ import annotations

import builtins
import socket
from dataclasses import dataclass

import pytest

from nanobot.capabilities.task_types import Attachment, TaskTyping, extract


@dataclass
class Mime:
    mime: str


def types(text: str = "", attachments=()) -> frozenset[str]:
    return extract(text, attachments).types


@pytest.mark.parametrize(("attachment", "expected"), [
    (Attachment(mime="application/pdf"), "file:pdf"),
    (Attachment(name="a.PDF"), "file:pdf"),
    (Mime("image/heic"), "file:image"),
    (Attachment(name="x.webp"), "file:image"),
    (Attachment(mime="audio/x-weird"), "file:audio"),
    (Attachment(name="v.flac"), "file:audio"),
    (Attachment(mime="video/mp2t"), "file:video"),
    (Attachment(name="v.mkv"), "file:video"),
    (Attachment(name="b.tar.gz"), "file:archive"),
    (Attachment(name="b.7z"), "file:archive"),
    (Attachment(name="d.odt"), "file:office"),
    (Attachment(name="s.xlsx"), "file:office"),
    (Attachment(name="t.csv"), "data/csv"),
    (Attachment(name="t.json"), "data/json"),
    (Attachment(name="t.yml"), "data/yaml"),
    (Attachment(name="t.md"), "text/markdown"),
    (Attachment(name="t.py"), "text/code:python"),
    (Attachment(name="t.sh"), "text/code:shell"),
    (Attachment(name="t.log"), "text/log"),
])
def test_attachments(attachment, expected):
    assert types("", [attachment]) == {expected}


def test_attachment_with_unknown_kind_adds_nothing():
    assert types("", [Attachment(name="x.bin", mime="application/octet-stream")]) == set()


def test_attachment_dicts_and_plain_names_are_accepted():
    assert types("", [{"name": "a.pdf"}, "b.png"]) == {"file:pdf", "file:image"}


@pytest.mark.parametrize(("text", "expected"), [
    ("see https://github.com/HKUDS/nanobot", {"url/git"}),
    ("see https://gitlab.com/a/b", {"url/git"}),
    ("see https://bitbucket.org/a/b", {"url/git"}),
    ("clone https://example.com/repo.git now", {"url/git"}),
    ("watch https://www.youtube.com/watch?v=x", {"url/video"}),
    ("watch https://youtu.be/x", {"url/video"}),
    ("watch https://vimeo.com/1", {"url/video"}),
    ("read http://example.com/post", {"url/web"}),
    ("read https://example.com/paper.pdf.", {"url/web", "file:pdf"}),
    ("code https://github.com/a/b/blob/main/x.py", {"url/git", "text/code:python"}),
])
def test_urls(text, expected):
    assert types(text) == expected


@pytest.mark.parametrize(("text", "expected"), [
    ("look at /var/log/syslog", {"path"}),
    ("look at ~/notes/plan.md", {"path", "text/markdown"}),
    ("open /tmp/data.CSV, please", {"path", "data/csv"}),
    ("and/or 1/2 km/h", set()),
    ("a url https://example.com/a/b.csv is not a path", {"url/web", "data/csv"}),
])
def test_paths(text, expected):
    assert types(text) == expected


@pytest.mark.parametrize(("text", "expected"), [
    ("```python\nx = 1\n```", {"text/code:python"}),
    ("```bash\nls\n```", {"text/code:shell"}),
    ("```sh\nls\n```", {"text/code:shell"}),
    ("```shell\nls\n```", {"text/code:shell"}),
    ("```json\n{}\n```", {"data/json"}),
    ("```rust\nfn main(){}\n```", {"text/code"}),
    ("```\nplain\n```", {"text/code"}),
    ("```python\nx\n```\nthen\n```json\n{}\n```", {"text/code:python", "data/json"}),
])
def test_fenced_code(text, expected):
    assert types(text) == expected


def test_type_hints():
    assert types("go $type:repo:git and $type:bogus") == {"repo:git"}


@pytest.mark.parametrize(("text", "expected"), [
    ("give it to me as a csv", "data/csv"),
    ("export INTO JSON", "data/json"),
    ("write it in yaml", "data/yaml"),
    ("summarise as markdown", "text/markdown"),
    ("save to pdf", "file:pdf"),
    ("show it in a table", "data/csv"),
    ("draw it as a chart", "file:image"),
    ("render to an image", None),  # "(a )?" only: "an" does not match
    ("render to image", "file:image"),
    ("as csv, no wait, as json", "data/json"),
    ("has json", None),
    ("nothing requested", None),
    ("as csv $out:text/markdown", "text/markdown"),
    ("as csv $out:bogus", "data/csv"),
])
def test_requested_output(text, expected):
    assert extract(text).requested_output == expected


def test_ambient_types_are_not_added():
    assert extract("hello there").types == frozenset()


def test_returns_a_frozen_task_typing():
    typing = extract("as json", [Attachment(name="a.pdf")])
    assert isinstance(typing, TaskTyping)
    assert isinstance(typing.types, frozenset)
    with pytest.raises(AttributeError):
        typing.requested_output = None  # type: ignore[misc]


def test_pure_no_filesystem_or_network(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("extract touched the filesystem or network")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    real = tmp_path / "real.pdf"
    text = f"see {real} and https://github.com/a/b as json"
    assert extract(text) == extract(text)
    assert extract(text).types == {"path", "file:pdf", "url/git"}
