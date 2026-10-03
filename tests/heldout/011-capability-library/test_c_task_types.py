"""Part C: deterministic task typing (FR-005). One table per rule."""

from __future__ import annotations

import builtins
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from _h011 import att, cap


def _x(text="", attachments=()):
    return cap("task_types").extract(text, attachments=tuple(attachments))


EXT_TYPES = [
    (".pdf", "file:pdf"),
    (".png", "file:image"), (".jpg", "file:image"), (".jpeg", "file:image"),
    (".gif", "file:image"), (".webp", "file:image"),
    (".mp3", "file:audio"), (".wav", "file:audio"), (".ogg", "file:audio"),
    (".m4a", "file:audio"), (".flac", "file:audio"),
    (".mp4", "file:video"), (".mkv", "file:video"), (".mov", "file:video"),
    (".webm", "file:video"),
    (".zip", "file:archive"), (".tar", "file:archive"), (".gz", "file:archive"),
    (".tgz", "file:archive"), (".7z", "file:archive"),
    (".docx", "file:office"), (".xlsx", "file:office"), (".pptx", "file:office"),
    (".doc", "file:office"), (".xls", "file:office"), (".ppt", "file:office"),
    (".odt", "file:office"),
    (".csv", "data/csv"), (".json", "data/json"), (".yaml", "data/yaml"), (".yml", "data/yaml"),
    (".md", "text/markdown"), (".py", "text/code:python"), (".sh", "text/code:shell"),
    (".log", "text/log"),
]


@pytest.mark.fr("FR-005")
def test_result_shape():
    tt = cap("task_types")
    r = _x("hello")
    assert isinstance(r, tt.TaskTyping)
    assert isinstance(r.types, frozenset)
    assert r.requested_output is None


@pytest.mark.fr("FR-005")
@pytest.mark.parametrize("text", ["", "hello there", "what is the capital of France?"])
def test_no_signal_no_types_and_no_ambient(text):
    r = _x(text)
    assert r.types == frozenset()
    assert r.requested_output is None


# -- attachments ------------------------------------------------------------------------------


@pytest.mark.fr("FR-005")
@pytest.mark.parametrize("ext, expected", EXT_TYPES)
def test_attachment_by_name(ext, expected):
    assert _x("", [att(name=f"upload{ext}")]).types == {expected}


@pytest.mark.fr("FR-005")
@pytest.mark.parametrize("mime, expected", [
    ("application/pdf", "file:pdf"),
    ("image/png", "file:image"), ("image/jpeg", "file:image"), ("image/svg+xml", "file:image"),
    ("audio/mpeg", "file:audio"), ("audio/x-wav", "file:audio"),
    ("video/mp4", "file:video"), ("video/quicktime", "file:video"),
])
def test_attachment_by_mime(mime, expected):
    assert _x("", [att(mime=mime)]).types == {expected}


@pytest.mark.fr("FR-005")
def test_attachment_mime_and_name_agree():
    assert _x("", [att(mime="application/pdf", name="r.pdf")]).types == {"file:pdf"}


@pytest.mark.fr("FR-005")
def test_attachment_with_none_mime_uses_name():
    from types import SimpleNamespace

    assert _x("", [SimpleNamespace(mime=None, name="t.csv")]).types == {"data/csv"}


@pytest.mark.fr("FR-005")
@pytest.mark.parametrize("a", [att(mime="application/octet-stream", name="blob.bin"),
                               att(name="README"), att(mime="text/plain")])
def test_unknown_attachment_adds_nothing(a):
    assert _x("", [a]).types == frozenset()


@pytest.mark.fr("FR-005")
def test_several_attachments_union():
    r = _x("", [att(name="a.pdf"), att(mime="image/png"), att(name="t.csv")])
    assert r.types == {"file:pdf", "file:image", "data/csv"}


# -- URLs ---------------------------------------------------------------------------------------


@pytest.mark.fr("FR-005")
@pytest.mark.parametrize("text, expected", [
    ("see https://github.com/acme/widget", {"url/git"}),
    ("clone https://gitlab.com/acme/widget please", {"url/git"}),
    ("https://bitbucket.org/acme/widget", {"url/git"}),
    ("fetch https://example.com/acme/widget.git", {"url/git"}),
    ("watch https://www.youtube.com/watch?v=abc123", {"url/video"}),
    ("https://youtu.be/abc123", {"url/video"}),
    ("https://vimeo.com/123456", {"url/video"}),
    ("read http://example.com/page", {"url/web"}),
    ("docs at https://docs.python.org/3/library/", {"url/web"}),
    ("look at https://github.com/acme/widget/blob/main/tool.py", {"url/git", "text/code:python"}),
])
def test_urls(text, expected):
    assert _x(text).types == expected


@pytest.mark.fr("FR-005")
@pytest.mark.parametrize("text, ext_type", [
    ("download https://example.com/files/report.pdf", "file:pdf"),
    ("https://example.com/data/table.csv", "data/csv"),
    ("https://example.com/dist/pkg.tar.gz", "file:archive"),
    ("https://cdn.example.org/a/b/clip.mp4", "file:video"),
])
def test_url_with_file_extension_adds_extension_type(text, ext_type):
    types = _x(text).types
    assert ext_type in types
    assert "path" not in types
    assert types <= {ext_type, "url/web"}


@pytest.mark.fr("FR-005")
def test_two_urls_union():
    r = _x("compare https://github.com/a/b with https://example.com/x")
    assert r.types == {"url/git", "url/web"}


# -- paths --------------------------------------------------------------------------------------


@pytest.mark.fr("FR-005")
@pytest.mark.parametrize("text, expected", [
    ("open /home/u/report.pdf now", {"file:pdf", "path"}),
    ("check ~/notes/todo.md", {"text/markdown", "path"}),
    ("look at /etc/hosts", {"path"}),
    ("scan /var/log/app/service.log", {"text/log", "path"}),
    ("load ~/data/table.csv", {"data/csv", "path"}),
    ("run /opt/tools/deploy.sh", {"text/code:shell", "path"}),
    ("the dir ~/projects/thing", {"path"}),
    ("/srv/img/photo.jpeg", {"file:image", "path"}),
])
def test_paths(text, expected):
    assert _x(text).types == expected


@pytest.mark.fr("FR-005")
@pytest.mark.parametrize("text", ["see docs/report.pdf", "use read/write access",
                                  "a 50/50 split"])
def test_non_absolute_text_is_not_a_path(text):
    assert _x(text).types == frozenset()


@pytest.mark.fr("FR-005")
def test_nonexistent_path_is_still_typed(tmp_path):
    missing = tmp_path / "definitely" / "missing.pdf"
    assert not missing.exists()
    assert _x(f"summarise {missing}").types == {"file:pdf", "path"}


# -- fences -------------------------------------------------------------------------------------


@pytest.mark.fr("FR-005")
@pytest.mark.parametrize("lang, expected", [
    ("python", "text/code:python"),
    ("bash", "text/code:shell"), ("sh", "text/code:shell"), ("shell", "text/code:shell"),
    ("json", "data/json"),
    ("", "text/code"), ("rust", "text/code"), ("javascript", "text/code"),
])
def test_fences(lang, expected):
    text = f"fix this:\n```{lang}\nsome code here\n```\nthanks"
    assert _x(text).types == {expected}


@pytest.mark.fr("FR-005")
def test_two_fences_union():
    text = "```python\nx = 1\n```\nand\n```json\n{\"a\": 1}\n```\n"
    assert _x(text).types == {"text/code:python", "data/json"}


# -- explicit hints -----------------------------------------------------------------------------


@pytest.mark.fr("FR-005")
@pytest.mark.parametrize("text, expected", [
    ("$type:data/yaml please", {"data/yaml"}),
    ("treat as $type:file:pdf", {"file:pdf"}),
    ("$type:repo:git", {"repo:git"}),
    ("$type:workspace and $type:url/web", {"workspace", "url/web"}),
    ("$type:bogus/thing", set()),
    ("$type:file:nope", set()),
    ("$type: data/csv", set()),
])
def test_type_hints(text, expected):
    assert _x(text).types == expected


# -- requested output ---------------------------------------------------------------------------


@pytest.mark.fr("FR-005")
@pytest.mark.parametrize("text, expected", [
    ("give it to me as csv", "data/csv"),
    ("return it as a csv", "data/csv"),
    ("dump AS JSON", "data/json"),
    ("convert this to yaml", "data/yaml"),
    ("turn it into markdown", "text/markdown"),
    ("write it in pdf", "file:pdf"),
    ("show results as a table", "data/csv"),
    ("plot it as a chart", "file:image"),
    ("render it as image", "file:image"),
    ("Render It As A Chart", "file:image"),
    ("export into a JSON", "data/json"),
])
def test_requested_output(text, expected):
    r = _x(text)
    assert r.requested_output == expected
    assert r.types == frozenset()


@pytest.mark.fr("FR-005")
@pytest.mark.parametrize("text, expected", [
    ("export as json, no wait, as csv", "data/csv"),
    ("as csv first then into markdown", "text/markdown"),
    ("to yaml or to json", "data/json"),
])
def test_requested_output_last_match_wins(text, expected):
    assert _x(text).requested_output == expected


@pytest.mark.fr("FR-005")
@pytest.mark.parametrize("text, expected", [
    ("as json $out:data/yaml", "data/yaml"),
    ("$out:data/yaml then as json", "data/yaml"),
    ("$out:file:pdf", "file:pdf"),
    ("as json $out:bogus", "data/json"),
    ("$out:nope/nope", None),
])
def test_out_hint_overrides_when_valid(text, expected):
    r = _x(text)
    assert r.requested_output == expected
    assert r.types == frozenset()


@pytest.mark.fr("FR-005")
def test_no_requested_output():
    assert _x("summarise this please").requested_output is None


# -- combined, purity, determinism ------------------------------------------------------------


@pytest.mark.fr("FR-005")
def test_combined_rules():
    text = ("Compare https://github.com/acme/widget with ~/notes/plan.md and\n"
            "```python\nprint(1)\n```\n$type:host then give it to me as a table")
    r = _x(text, [att(mime="application/pdf", name="spec.pdf")])
    assert r.types == {"url/git", "text/markdown", "path", "text/code:python", "host",
                       "file:pdf"}
    assert r.requested_output == "data/csv"


PURITY_TEXT = ("summarise ~/docs/report.pdf and https://example.com/a.csv as markdown\n"
               "```bash\nls\n```")
PURITY_ATT = (att(mime="image/png", name="shot.png"), att(name="notes.yaml"))


@pytest.mark.fr("FR-005")
def test_pure_no_filesystem_no_network():
    extract = cap("task_types").extract
    expected = extract(PURITY_TEXT, attachments=PURITY_ATT)  # warm-up (lazy module state)

    def boom(*a, **k):
        raise AssertionError("extract touched the filesystem or network")

    saved = (builtins.open, os.stat, os.lstat, os.listdir, os.scandir, os.path.exists,
             Path.exists, Path.is_file, Path.stat, socket.socket, socket.create_connection,
             socket.getaddrinfo)
    builtins.open = os.stat = os.lstat = os.listdir = os.scandir = boom
    os.path.exists = boom
    Path.exists = Path.is_file = Path.stat = boom
    socket.socket = socket.create_connection = socket.getaddrinfo = boom
    try:
        got = extract(PURITY_TEXT, attachments=PURITY_ATT)
    finally:
        (builtins.open, os.stat, os.lstat, os.listdir, os.scandir, os.path.exists,
         Path.exists, Path.is_file, Path.stat, socket.socket, socket.create_connection,
         socket.getaddrinfo) = saved
    assert got == expected
    core = {"file:pdf", "path", "data/csv", "text/code:shell", "file:image", "data/yaml"}
    assert core <= got.types <= core | {"url/web"}
    assert got.requested_output == "text/markdown"


@pytest.mark.fr("FR-005")
def test_deterministic_in_process():
    extract = cap("task_types").extract
    results = [extract(PURITY_TEXT, attachments=PURITY_ATT) for _ in range(5)]
    assert all(r == results[0] for r in results)


@pytest.mark.fr("FR-005")
def test_does_not_mutate_inputs():
    a = att(mime="application/pdf", name="r.pdf")
    before = dict(vars(a))
    attachments = [a]
    cap("task_types").extract("x", attachments=attachments)
    assert vars(a) == before and attachments == [a]


_SUBPROC = r"""
import json, sys
from types import SimpleNamespace
from nanobot.capabilities.task_types import extract
r = extract(sys.argv[1], attachments=(SimpleNamespace(mime="image/png", name="shot.png"),
                                      SimpleNamespace(name="notes.yaml")))
print(json.dumps([sorted(r.types), r.requested_output]))
"""


@pytest.mark.fr("FR-005")
def test_deterministic_across_processes():
    outs = set()
    for seed in ("0", "1", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONDONTWRITEBYTECODE": "1"}
        proc = subprocess.run([sys.executable, "-c", _SUBPROC, PURITY_TEXT], env=env,
                              capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr[-2000:]
        outs.add(proc.stdout.strip())
    assert len(outs) == 1
