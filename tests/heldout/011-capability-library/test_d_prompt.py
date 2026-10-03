"""Part D: prompt integration, trace, fingerprint and index wiring (FR-010, FR-011, FR-012,
FR-015, FR-004 default path, SC-001, SC-002 through the prompt).

Two surfaces: a ``ContextBuilder`` over a fixture workspace (``_h011.make_ctx``: the
``capabilities`` config reaches it through its host env's ``config.section("capabilities")``,
and through a constructor keyword when it has one), and a kernel built from a ``Config``
(``moeka.Environment.from_config``) whose provider-visible system prompt is read from the
``FakeProvider``.
"""

from __future__ import annotations

import pytest
from _h011 import (
    GIT_QUERY,
    GOLDEN_SC001,
    PDF_QUERY,
    agent_of,
    build_library,
    build_prompt,
    canonical_golden,
    ev,
    make_ctx,
    normalise_prompt,
    sc001_fixture,
    strip,
    summary_lines,
    summary_names,
    system_prompt_of,
    user_message_of,
    write_skill,
)

ALL_EFFECTS = ["none", "read-workspace", "write-workspace", "network", "exec", "sudo"]
WEIRD_ALL = {"selection": "all", "top_k": 1, "ambient_types": ["host"], "allowed_effects": ["none"]}
PDF_OK = {"pdf-summarizer", "notes-helper"}  # skills that may survive PDF_QUERY (always excluded)


def _filtered(k=3, **extra):
    return {"selection": "filtered", "top_k": k, **extra}


# -- Config -------------------------------------------------------------------------------------


@pytest.mark.fr("FR-010")
def test_config_defaults():
    from nanobot.config.schema import Config

    c = Config().capabilities
    assert c.selection == "all"
    assert c.top_k == 8
    assert list(c.ambient_types) == ["workspace"]
    assert sorted(c.allowed_effects) == sorted(ALL_EFFECTS)


@pytest.mark.fr("FR-010")
def test_config_accepts_camel_and_snake():
    from nanobot.config.schema import Config

    a = Config.model_validate({"capabilities": {"selection": "filtered", "topK": 5,
                                                "ambientTypes": ["workspace", "host"],
                                                "allowedEffects": ["none"]}}).capabilities
    b = Config(capabilities={"selection": "filtered", "top_k": 5,
                             "ambient_types": ["workspace", "host"],
                             "allowed_effects": ["none"]}).capabilities
    assert (a.selection, a.top_k, list(a.ambient_types), list(a.allowed_effects)) == (
        "filtered", 5, ["workspace", "host"], ["none"])
    assert a == b


@pytest.mark.fr("FR-010")
@pytest.mark.parametrize("bad", [{"top_k": 0}, {"top_k": 65}, {"top_k": -3},
                                 {"selection": "some"}, {"selection": "FILTERED-ish"}])
def test_config_rejects_invalid(bad):
    from pydantic import ValidationError

    from nanobot.config.schema import Config

    assert Config(capabilities={"top_k": 8}).capabilities.top_k == 8  # the block exists
    with pytest.raises(ValidationError):
        Config(capabilities=bad)


@pytest.mark.fr("FR-010")
@pytest.mark.parametrize("k", [1, 64])
def test_config_top_k_bounds_inclusive(k):
    from nanobot.config.schema import Config

    assert Config(capabilities={"top_k": k}).capabilities.top_k == k


# -- SC-001: "all" changes nothing --------------------------------------------------------------


def _golden_check(tmp_path, caps):
    builtin, work, state = sc001_fixture(tmp_path)
    ctx = make_ctx(work, state, caps=caps, builtin=builtin)
    got = normalise_prompt(build_prompt(ctx, PDF_QUERY), tmp_path)
    golden = GOLDEN_SC001.read_text(encoding="utf-8")
    assert canonical_golden(got) == canonical_golden(golden)


@pytest.mark.fr("SC-001", "FR-010")
def test_sc001_golden_default_config(tmp_path):
    """The prompt equals the one the pre-feature tree rendered for the same fixture (golden
    captured from the spec commit before the feature; see SPEC-MAP)."""
    _golden_check(tmp_path, None)


@pytest.mark.fr("SC-001", "FR-010")
@pytest.mark.parametrize("caps", [{"selection": "all"}, WEIRD_ALL])
def test_sc001_golden_explicit_all(tmp_path, caps):
    _golden_check(tmp_path, caps)


@pytest.mark.fr("SC-001", "FR-010")
@pytest.mark.parametrize("text", [None, "", PDF_QUERY, GIT_QUERY, "use $csv-stats now"])
def test_sc001_all_is_byte_identical_to_default(tmp_path, text):
    builtin, work, state = sc001_fixture(tmp_path)
    base = build_prompt(make_ctx(work, state, builtin=builtin), text)
    for caps in ({"selection": "all"}, WEIRD_ALL,
                 {"selection": "all", "top_k": 64, "ambient_types": [],
                  "allowed_effects": ALL_EFFECTS}):
        got = build_prompt(make_ctx(work, state, caps=caps, builtin=builtin), text)
        assert got == base, caps


@pytest.mark.fr("SC-001", "FR-010")
def test_sc001_kernel_prompt_identical(kmaker, tmp_path):
    builtin, work = build_library(tmp_path / "lib")
    k0, f0 = kmaker(builtin=builtin, work=work)
    k1, f1 = kmaker(caps=WEIRD_ALL, builtin=builtin, work=work)
    agent_of(k0).run_sync(PDF_QUERY)
    agent_of(k1).run_sync(PDF_QUERY)
    assert system_prompt_of(f1) == system_prompt_of(f0)


@pytest.mark.fr("SC-001", "FR-011")
def test_all_mode_emits_no_capability_selected(tmp_path, sink):
    builtin, work = build_library(tmp_path / "lib")
    for caps in (None, WEIRD_ALL):
        build_prompt(make_ctx(work, tmp_path / "state", caps=caps, builtin=builtin, trace=sink),
                     PDF_QUERY)
    assert ev(sink, "skill.listed")
    assert ev(sink, "capability.selected") == []


# -- FR-010 filtered (ContextBuilder) -----------------------------------------------------------


def _ctx(tmp_path, caps, sink=None):
    builtin, work = build_library(tmp_path / "lib")
    return make_ctx(work, tmp_path / "state", caps=caps, builtin=builtin, trace=sink), builtin, work


@pytest.mark.fr("FR-010", "SC-002")
def test_filtered_lists_at_most_k_and_respects_types(tmp_path):
    ctx, _, _ = _ctx(tmp_path, _filtered(3))
    prompt = build_prompt(ctx, PDF_QUERY)
    names = summary_names(prompt)
    assert len(names) <= 3
    assert "pdf-summarizer" in names
    assert set(names) <= PDF_OK
    for excluded in ("git-reviewer", "needs-bin", "csv-stats", "ws-pdf-tagger",
                     "office-converter", "video-clipper"):
        assert excluded not in names


@pytest.mark.fr("FR-010")
def test_filtered_keeps_active_skills_section(tmp_path):
    ctx, _, _ = _ctx(tmp_path, _filtered(3))
    prompt = build_prompt(ctx, PDF_QUERY)
    assert "# Active Skills" in prompt and "ALWAYS-GUIDE-BODY" in prompt
    assert "always-guide" not in summary_names(prompt)


@pytest.mark.fr("FR-010")
def test_filtered_line_format_unchanged(tmp_path):
    builtin, work = build_library(tmp_path / "lib")
    full = build_prompt(make_ctx(work, tmp_path / "s0", builtin=builtin), GIT_QUERY)
    filt = build_prompt(make_ctx(work, tmp_path / "s1", caps=_filtered(4), builtin=builtin),
                        GIT_QUERY)
    lines = summary_lines(filt)
    assert lines
    for line in lines:
        assert line in full.splitlines(), line
    assert "# Skills" in filt


@pytest.mark.fr("FR-010", "FR-007")
def test_filtered_unavailable_skill_keeps_marker(tmp_path):
    ctx, _, _ = _ctx(tmp_path, _filtered(4))
    prompt = build_prompt(ctx, GIT_QUERY)
    names = summary_names(prompt)
    assert "git-reviewer" in names and "needs-bin" in names
    assert "web-reader" not in names and "pdf-summarizer" not in names
    [line] = [ln for ln in summary_lines(prompt) if "**needs-bin**" in ln]
    assert "(unavailable" in line


@pytest.mark.fr("FR-010")
def test_explicit_skill_bypasses_filter_and_is_not_counted(tmp_path):
    ctx, _, _ = _ctx(tmp_path, _filtered(2))
    text = "Use $csv-stats on ~/docs/report.pdf and summarise it as markdown"
    prompt = build_prompt(ctx, text)
    others = [n for n in summary_names(prompt) if n != "csv-stats"]
    assert len(others) <= 2 and "pdf-summarizer" in others
    msg = ctx.build_current_message(text)
    assert "Do csv-stats." in str(msg["content"])


@pytest.mark.fr("FR-010")
def test_filtered_query_changes_listing(tmp_path):
    ctx, _, _ = _ctx(tmp_path, _filtered(3))
    a = set(summary_names(build_prompt(ctx, PDF_QUERY)))
    b = set(summary_names(build_prompt(ctx, GIT_QUERY)))
    assert "pdf-summarizer" in a and "pdf-summarizer" not in b
    assert "git-reviewer" in b and "git-reviewer" not in a


@pytest.mark.fr("FR-010", "FR-007")
def test_filtered_allowed_effects_from_config(tmp_path):
    caps = _filtered(3, allowed_effects=["none", "exec"])
    ctx, _, _ = _ctx(tmp_path, caps)
    names = summary_names(build_prompt(ctx, GIT_QUERY))
    assert "git-reviewer" not in names  # declares network


@pytest.mark.fr("FR-010", "FR-007")
def test_filtered_ambient_types_from_config(tmp_path):
    builtin, work = build_library(tmp_path / "lib")
    write_skill(builtin, "host-admin", "Administer the host machine",
                io={"inputs": ["host"], "outputs": ["none"], "effects": ["none"]})
    q = "administer the host machine"
    off = make_ctx(work, tmp_path / "s0", caps=_filtered(3), builtin=builtin)
    on = make_ctx(work, tmp_path / "s1", caps=_filtered(3, ambient_types=["workspace", "host"]),
                  builtin=builtin)
    assert "host-admin" not in summary_names(build_prompt(off, q))
    assert "host-admin" in summary_names(build_prompt(on, q))


@pytest.mark.fr("FR-009", "FR-010")
def test_filtered_empty_fallback_is_todays_listing(tmp_path):
    builtin = tmp_path / "bskills"
    work = tmp_path / "work"
    for i in range(10):
        write_skill(builtin, f"git-only-{i}", f"git helper {i}",
                    io={"inputs": ["url/git"], "outputs": ["text/markdown"], "effects": ["none"]})
    full = build_prompt(make_ctx(work, tmp_path / "s0", builtin=builtin), "hello there")
    filt = build_prompt(make_ctx(work, tmp_path / "s1", caps=_filtered(3), builtin=builtin),
                        "hello there")
    assert filt == full


@pytest.mark.fr("FR-009", "FR-010")
def test_filtered_small_fallback_is_todays_listing(tmp_path):
    builtin = tmp_path / "bskills"
    work = tmp_path / "work"
    write_skill(builtin, "git-a", "git a", io={"inputs": ["url/git"], "effects": ["none"]})
    write_skill(builtin, "pdf-b", "pdf b", io={"inputs": ["file:pdf"], "effects": ["none"]})
    write_skill(builtin, "plain-c", "plain c")
    full = build_prompt(make_ctx(work, tmp_path / "s0", builtin=builtin), PDF_QUERY)
    filt = build_prompt(make_ctx(work, tmp_path / "s1", caps=_filtered(8), builtin=builtin),
                        PDF_QUERY)
    assert filt == full


# -- FR-011 trace -------------------------------------------------------------------------------

SELECTED_KEYS = {"mode", "fallback", "task_types", "requested_output", "survivors", "selected"}


@pytest.mark.fr("FR-011")
def test_capability_selected_event(tmp_path, sink):
    ctx, _, _ = _ctx(tmp_path, _filtered(3), sink)
    prompt = build_prompt(ctx, PDF_QUERY)
    [event] = ev(sink, "capability.selected")
    body = strip(event)
    assert set(body) == SELECTED_KEYS
    assert body["mode"] == "filtered" and body["fallback"] is None
    assert body["task_types"] == sorted(body["task_types"])
    assert {"text/query", "workspace", "file:pdf", "path"} <= set(body["task_types"])
    assert body["requested_output"] == "text/markdown"
    assert isinstance(body["survivors"], int) and not isinstance(body["survivors"], bool)
    assert isinstance(body["selected"], list) and len(body["selected"]) <= 3
    assert body["survivors"] >= len(body["selected"])
    assert "pdf-summarizer" in body["selected"]
    listed = summary_names(prompt)
    assert set(listed) <= set(body["selected"])
    [skill_listed] = ev(sink, "skill.listed")
    assert set(skill_listed["skills"]) - set(skill_listed.get("active") or []) == set(listed)


@pytest.mark.fr("FR-011", "FR-009")
def test_capability_selected_reports_empty_fallback(tmp_path, sink):
    builtin = tmp_path / "bskills"
    for i in range(10):
        write_skill(builtin, f"git-only-{i}", "git helper",
                    io={"inputs": ["url/git"], "effects": ["none"]})
    ctx = make_ctx(tmp_path / "work", tmp_path / "state", caps=_filtered(3), builtin=builtin,
                   trace=sink)
    build_prompt(ctx, "hello there")
    [event] = ev(sink, "capability.selected")
    assert event["fallback"] == "empty" and event["survivors"] == 0
    assert event["requested_output"] is None
    assert sorted(event["selected"]) == sorted(f"git-only-{i}" for i in range(10))


@pytest.mark.fr("FR-011")
def test_no_event_when_trace_is_off(tmp_path, sink):
    ctx, _, _ = _ctx(tmp_path, _filtered(3), sink)
    build_prompt(ctx, PDF_QUERY, trace=False)
    assert ev(sink, "capability.selected") == [] and ev(sink, "skill.listed") == []


@pytest.mark.fr("FR-011")
def test_one_event_per_build(tmp_path, sink):
    ctx, _, _ = _ctx(tmp_path, _filtered(3), sink)
    build_prompt(ctx, PDF_QUERY)
    build_prompt(ctx, GIT_QUERY)
    events = ev(sink, "capability.selected")
    assert len(events) == 2
    assert "url/git" in events[1]["task_types"] and "file:pdf" not in events[1]["task_types"]


# -- FR-010 / FR-011 through a kernel run -------------------------------------------------------


@pytest.mark.fr("FR-010", "FR-011", "SC-002")
def test_kernel_filtered_run(kmaker, tmp_path, sink):
    builtin, work = build_library(tmp_path / "lib")
    kernel, fake = kmaker(caps=_filtered(8), builtin=builtin, work=work)
    result = agent_of(kernel).run_sync(PDF_QUERY)
    assert fake.calls, result
    prompt = system_prompt_of(fake)
    names = summary_names(prompt)
    assert names and len(names) <= 8
    assert "pdf-summarizer" in names and set(names) <= PDF_OK
    assert "ALWAYS-GUIDE-BODY" in prompt
    [event] = ev(sink, "capability.selected")
    assert event["mode"] == "filtered" and len(event["selected"]) <= 8
    assert {"text/query", "workspace", "file:pdf", "path"} <= set(event["task_types"])
    assert set(names) <= set(event["selected"])


@pytest.mark.fr("FR-010")
def test_kernel_explicit_skill_still_loaded(kmaker, tmp_path):
    builtin, work = build_library(tmp_path / "lib")
    kernel, fake = kmaker(caps=_filtered(2), builtin=builtin, work=work)
    agent_of(kernel).run_sync("Use $csv-stats on ~/docs/report.pdf as markdown")
    assert "Do csv-stats." in user_message_of(fake)
    others = [n for n in summary_names(system_prompt_of(fake)) if n != "csv-stats"]
    assert len(others) <= 2


@pytest.mark.fr("FR-004", "FR-015")
def test_kernel_index_lives_in_state_data_dir(kmaker, tmp_path):
    builtin, work = build_library(tmp_path / "lib")
    kernel, _ = kmaker(caps=_filtered(3), builtin=builtin, work=work)
    agent_of(kernel).run_sync(PDF_QUERY)
    data_dir = kernel.env.paths.data_dir
    assert (data_dir / "capabilities.db").is_file()
    assert not list((work / "skills").rglob("capabilities.db*"))
    assert not list(work.rglob("capabilities.db*"))


# -- FR-012 fingerprint -------------------------------------------------------------------------


def _digest(kmaker, caps, builtin, work):
    kernel, _ = kmaker(caps=caps, builtin=builtin, work=work)
    return agent_of(kernel).fingerprint().digest


@pytest.mark.fr("FR-012")
def test_fingerprint_stable_for_same_config(kmaker, tmp_path):
    builtin, work = build_library(tmp_path / "lib")
    a = _digest(kmaker, _filtered(5), builtin, work)
    b = _digest(kmaker, _filtered(5), builtin, work)
    assert a == b
    explicit_default = {"selection": "all", "top_k": 8, "ambient_types": ["workspace"],
                        "allowed_effects": ALL_EFFECTS}
    assert _digest(kmaker, None, builtin, work) == _digest(kmaker, explicit_default, builtin, work)


@pytest.mark.fr("FR-012")
@pytest.mark.parametrize("left, right", [
    ({"selection": "all"}, {"selection": "filtered"}),
    (_filtered(8), _filtered(9)),
    ({"selection": "all", "top_k": 8}, {"selection": "all", "top_k": 3}),
    (_filtered(64), _filtered(63)),
    (_filtered(8), _filtered(8, ambient_types=["workspace", "host"])),
    (_filtered(8), _filtered(8, allowed_effects=["none", "read-workspace"])),
    ({"selection": "all"}, {"selection": "all", "allowed_effects": ["none"]}),
    ({"selection": "all"}, {"selection": "all", "ambient_types": []}),
])
def test_fingerprint_differs_when_capabilities_config_differs(kmaker, tmp_path, left, right):
    """Includes pairs whose rendered prompts are identical (top_k above the library size,
    "all" with different filter settings): the config block itself is fingerprinted."""
    builtin, work = build_library(tmp_path / "lib")
    assert _digest(kmaker, left, builtin, work) != _digest(kmaker, right, builtin, work)


@pytest.mark.fr("FR-012", "FR-011")
def test_fingerprint_emits_no_capability_selected(kmaker, tmp_path, sink):
    builtin, work = build_library(tmp_path / "lib")
    kernel, _ = kmaker(caps=_filtered(3), builtin=builtin, work=work)
    agent_of(kernel).fingerprint()
    assert ev(sink, "capability.selected") == []


# -- FR-015 degraded index ----------------------------------------------------------------------


def _capability_warnings(logs):
    return [m for level, m in logs if "capabilit" in m.lower()]


@pytest.mark.fr("FR-015")
def test_kernel_unusable_index_degrades_to_all(kmaker, tmp_path, logs):
    builtin, work = build_library(tmp_path / "lib")
    ref_kernel, ref_fake = kmaker(builtin=builtin, work=work)
    agent_of(ref_kernel).run_sync(PDF_QUERY)
    kernel, fake = kmaker(caps=_filtered(3), builtin=builtin, work=work)
    blocker = kernel.env.paths.data_dir / "capabilities.db"
    blocker.mkdir(parents=True)  # a directory where the database file must go
    (blocker / "keep").write_text("x")
    agent = agent_of(kernel)
    first = agent.run_sync(PDF_QUERY)
    second = agent.run_sync(GIT_QUERY)
    assert len(fake.calls) == 2, (first, second)
    assert system_prompt_of(fake, 0) == system_prompt_of(ref_fake, 0)
    assert len(summary_names(system_prompt_of(fake, 1))) == len(summary_names(
        system_prompt_of(ref_fake, 0)))
    assert len(_capability_warnings(logs)) == 1, logs


@pytest.mark.fr("FR-015")
def test_context_unusable_index_degrades_to_all(tmp_path, logs, sink):
    builtin, work = build_library(tmp_path / "lib")
    full = build_prompt(make_ctx(work, tmp_path / "s0", builtin=builtin), PDF_QUERY)
    state = tmp_path / "s1"
    blocker = state / "data" / "capabilities.db"
    blocker.mkdir(parents=True)
    ctx = make_ctx(work, state, caps=_filtered(3), builtin=builtin, trace=sink)
    assert build_prompt(ctx, PDF_QUERY) == full
    assert build_prompt(ctx, PDF_QUERY) == full
    assert len(_capability_warnings(logs)) == 1, logs


@pytest.mark.fr("FR-015")
def test_index_follows_skill_changes(tmp_path):
    """A skill added after the first build is selectable on the next build."""
    builtin, work = build_library(tmp_path / "lib")
    ctx = make_ctx(work, tmp_path / "state", caps=_filtered(3), builtin=builtin)
    q = "transcribe ~/media/talk.flac as markdown"
    first = summary_names(build_prompt(ctx, q))
    assert "flac-wizard" not in first
    write_skill(work / "skills", "flac-wizard", "Transcribe flac talk recordings as markdown",
                io={"inputs": ["file:audio"], "outputs": ["text/markdown"],
                    "effects": ["read-workspace"]})
    second = summary_names(build_prompt(ctx, q))
    assert "flac-wizard" in second and len(second) <= 3
