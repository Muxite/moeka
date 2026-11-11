"""Self-check gate helpers: duplicate detection, measured text and the honest report."""

from nanobot.creative_checks import (
    EVIDENCE_RULES,
    compose_report,
    find_duplicate,
    measured_text,
)

FACTS = {"width": 1080, "height": 1920, "duration_s": 20.3, "fps": 30.0, "has_audio": True,
         "loudness": {"integrated_lufs": -16.2, "true_peak_db": -1.1}}


def test_find_duplicate_returns_the_newest_earlier_match_only():
    rows = [{"id": 12, "ref": "a", "filename": "later"}, {"id": 11, "ref": "a", "filename": "self"},
            {"id": 10, "ref": "a", "filename": "reel-4", "created_at": "t"}, {"id": 9, "ref": "a", "filename": "first"},
            {"id": 8, "ref": "b", "filename": "other"}]
    assert find_duplicate("a", 11, rows) == {"id": 10, "file": "reel-4", "at": "t"}
    assert find_duplicate("zzz", 11, rows) is None
    assert find_duplicate("a", 9, [{"id": 9, "ref": "a"}]) is None  # never itself


def test_find_duplicate_ignores_malformed_rows():
    assert find_duplicate("a", 5, [{"id": "x", "ref": "a"}, {"ref": "a"}, {"id": 2}]) is None


def test_measured_text_full_partial_and_missing():
    assert measured_text({"facts": FACTS}) == "1080x1920, 20.3 s, 30.0 fps, audio -16.2 LUFS (peak -1.1 dB)"
    assert measured_text({"facts": {"width": 320, "height": 240, "has_audio": False}}) == "320x240, no audio"
    assert measured_text({"facts": None, "mb": 1.8}) == "1.8 MB"


def test_report_for_a_normal_delivery_lists_measured_and_unchecked():
    text = compose_report("Your reel is ready. Full file check: not yet run.",
                          {"sha": "0aa4e30e" + "0" * 56, "facts": FACTS, "duplicate_of": None})
    assert text.splitlines()[0] == "Your reel is ready."
    assert "Measured: 1080x1920, 20.3 s" in text and "File 0aa4e30e." in text
    assert "Not checked: caption legibility, timing and motion" in text
    assert "WARNING" not in text and "not yet run" not in text


def test_report_for_an_identical_rerender_leads_with_the_warning():
    text = compose_report("Ready.", {"sha": "ab" * 32, "facts": None, "mb": 1.8,
                                      "duplicate_of": {"id": 10, "file": "reel-4.mp4"}})
    assert text.splitlines()[1].startswith("WARNING: byte-identical to delivery 10 (reel-4.mp4)")
    assert "Measured: 1.8 MB." in text


def test_empty_wonhee_text_still_gets_a_sentence():
    assert compose_report("", {"sha": "c" * 64, "mb": 1}).startswith("Your wonhee result is ready.")


def test_the_agent_rules_forbid_unverified_fixed_claims():
    assert "NEW file" in EVIDENCE_RULES and "view_media" in EVIDENCE_RULES
    assert "duplicate_of" in EVIDENCE_RULES and "Never say 'fixed'" in EVIDENCE_RULES
