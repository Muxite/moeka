"""Group F: the tree cannot see the suite outside the run or reach the network; nothing of the
suite survives where the tree or implementer could read it (Story 2, SC-003).

Strategy: every toy suite carries a unique canary token in its own file text. After the run we
search the worktree, the scratch temp dir, HOME and the held-out root (minus the private report
dir) for that token or for any file the tree wrote, and assert the suite text and tree-written
exfil files appear only under the private report directory.
"""

from __future__ import annotations

import os
import socket
import threading
import time

import pytest

import _h8
from _h8 import load_report, search_tokens

pytestmark = pytest.mark.docker


def _survivors(h, token):
    roots = [h.tree, h.tmp, h.home, h.root]
    # the private report dirs may hold suite material; the suite's own source dir obviously does
    exclude = [h.reports, h.root / "_reports", h.suite]
    return search_tokens(roots, [token], exclude=exclude, since=h.t_start)


# -- sync-time exfiltration attempts -------------------------------------------------------------

@pytest.mark.fr("FR-004")
def test_sync_cannot_see_suite(h, image, no_new_containers):
    tok = h.tok
    # sync step tries to find and copy any suite material into /work
    h.make_tree({"h8sync.sh": '''#!/bin/sh
        found=""
        for d in /suite /out /heldout; do
            # an empty mount point (or the image's own /root) is not suite material
            if [ -d "$d" ] && [ -n "$(ls -A "$d" 2>/dev/null | grep -v '^\\.' )" ]; then found="$found $d"; fi
        done
        # search the whole fs for the token the suite carries
        hits=$(timeout 120 grep -rl --exclude-dir=proc --exclude-dir=sys --exclude-dir=dev "SUITE_CANARY" / 2>/dev/null | grep -v /work || true)
        printf '%s' "$found" > /work/_found_suite
        printf '%s' "$hits" > /work/_grep_hits
        chmod -R a+rX /work/_found_suite /work/_grep_hits 2>/dev/null
        exit 0
    '''})
    h.write_suite({"test_x.py": f'''
        import os
        import pytest

        # SUITE_CANARY {tok}
        @pytest.mark.fr("FR-004")
        def test_sync_saw_nothing():
            assert open("/work/_found_suite").read().strip() == ""
            assert open("/work/_grep_hits").read().strip() == ""
        '''})
    res = h.run_sb("--sync-cmd", "sh {copy}/h8sync.sh")
    rep = load_report(res.report_dir)
    _h8.assert_toys_passed(rep, ["test_sync_saw_nothing"])


# -- test-time exfiltration attempts -------------------------------------------------------------

EXFIL_SUITE = '''
import os
import socket
import pytest

# SUITE_CANARY {tok}
SECRET = "SUITE_CANARY {tok} assertion-secret-{tok}"


def _try(fn):
    try:
        fn()
        return True
    except Exception:
        return False


@pytest.mark.fr("FR-006")
def test_persist_attempts_do_not_escape():
    # try to write the suite text to every path that might outlive the run
    targets = ["/work/exfil-{tok}.txt", "/tmp/exfil-{tok}.txt", "/suite/exfil-{tok}.txt",
               "/out/exfil-{tok}.txt", "/root/exfil-{tok}.txt", "/home/exfil-{tok}.txt",
               "/dev/shm/exfil-{tok}.txt"]
    # the judge may write its own /out (copied only to the private report, FR-009)
    open("/out/judge-note-{tok}.txt", "w").write(SECRET)
    impl = os.environ["HELDOUT_IMPL"]
    import subprocess
    wrote = []
    for t in targets:
        cp = subprocess.run([impl, "sh", "-c", 'printf %s "$1" > "$2"', "x", SECRET, t],
                            capture_output=True, timeout=30)
        if cp.returncode == 0:
            wrote.append(t)
    # impl may write /work, /tmp, /dev/shm (removed with the run); never /suite, /out, /root, /home
    assert all(t.startswith(("/work/", "/tmp/", "/dev/shm/")) for t in wrote), wrote


@pytest.mark.fr("FR-006")
def test_no_network():
    for addr in (("1.1.1.1", 53), ("172.17.0.1", 9), ("8.8.8.8", 443)):
        s = socket.socket()
        s.settimeout(2)
        with pytest.raises(OSError):
            s.connect(addr)
        s.close()
    # DNS resolution must also fail (no resolver reachable)
    with pytest.raises(OSError):
        socket.getaddrinfo("example.com", 80)
'''


@pytest.fixture(scope="module")
def exfil(image):
    hx = _h8.H()
    try:
        hx.make_tree()
        hx.write_suite({"test_exfil.py": EXFIL_SUITE.format(tok=hx.tok)})
        res = hx.run_sb(counted=True)
        survivors = _survivors(hx, "SUITE_CANARY")
        yield dict(h=hx, res=res, rep=load_report(res.report_dir), survivors=survivors,
                   report_dir=res.report_dir)
    finally:
        hx.close()


@pytest.mark.fr("FR-006", "SC-003")
def test_in_container_exfil_blocked(exfil):
    _h8.assert_toys_passed(exfil["rep"], ["test_persist_attempts_do_not_escape", "test_no_network"])


@pytest.mark.fr("SC-003")
def test_no_suite_text_survives_outside_report(exfil):
    assert exfil["survivors"] == [], f"suite text survived at: {exfil['survivors']}"


@pytest.mark.fr("SC-003", "FR-009")
def test_scratch_and_worktree_clean(exfil):
    h = exfil["h"]
    assert h.leftovers() == [], "scratch leftovers remain"
    # worktree holds only the toy files, no exfil artefact
    files = set(_h8.snapshot(h.tree))
    assert not any("exfil" in f for f in files), files


@pytest.mark.fr("SC-003")
def test_token_present_in_report_dir(exfil):
    # sanity: the token DOES exist in the private report (proves the search would find leaks)
    hits = search_tokens([exfil["report_dir"]], ["SUITE_CANARY"])
    assert hits, "token not found even in the private report; search is ineffective"


# -- host-mount exfiltration: a suite cannot mount the host (no docker socket) -------------------

@pytest.mark.fr("FR-006", "FR-010")
def test_no_docker_socket_in_test_container(h, tree, image, no_new_containers):
    h.write_suite({"test_sock.py": '''
        import os
        import glob
        import pytest


        @pytest.mark.fr("FR-006")
        def test_no_docker_socket():
            assert not os.path.exists("/var/run/docker.sock")
            assert not os.path.exists("/run/docker.sock")
            for top in ("/var", "/run", "/tmp", "/work", "/srv", "/mnt"):
                assert not glob.glob(top + "/**/docker.sock", recursive=True)
            assert "docker.sock" not in open("/proc/self/mountinfo").read()
        '''})
    res = h.run_sb()
    _h8.assert_toys_passed(load_report(res.report_dir), ["test_no_docker_socket"])


# -- a tree that forged at sync time cannot leave the suite for its next run ---------------------

@pytest.mark.fr("SC-003", "FR-009")
def test_second_run_starts_clean(h, image, no_new_containers):
    tok = h.tok
    # first run: the tree writes a marker into /work during the test phase
    h.make_tree()
    h.write_suite({"test_mark.py": f'''
        import pytest

        # SUITE_CANARY {tok}
        @pytest.mark.fr("FR-001")
        def test_write_marker():
            with open("/work/persist-{tok}.txt", "w") as fh:
                fh.write("SUITE_CANARY {tok}")
        '''})
    h.run_sb(counted=False)
    # the worktree must be unchanged and no persist marker exists anywhere the tree reads
    assert _survivors(h, "SUITE_CANARY") == []
    files = set(_h8.snapshot(h.tree))
    assert not any("persist" in f for f in files)
