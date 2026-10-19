from __future__ import annotations

from pathlib import Path

from nanobot.agent.context import ContextBuilder
from nanobot.agent.skill_seeds import seed_portable_skills
from nanobot.agent.skills import SkillsLoader
from nanobot.capabilities.task_types import Attachment
from nanobot.config.schema import CapabilitiesConfig


def test_skill_seeds_install_missing_files_without_replacing_local_edits(tmp_path: Path) -> None:
    local = tmp_path / "skills" / "test-runner" / "SKILL.md"
    local.parent.mkdir(parents=True)
    local.write_text("owner edited this skill\n", encoding="utf-8")

    installed = seed_portable_skills(tmp_path)

    assert "test-runner" not in installed
    assert local.read_text(encoding="utf-8") == "owner edited this skill\n"
    for name in ("python-work", "structured-data", "notes", "document-processing", "media-processing"):
        assert (tmp_path / "skills" / name / "SKILL.md").is_file()
    assert seed_portable_skills(tmp_path) == []


def test_seeded_skills_validate_and_load_under_filtered_selection(tmp_path: Path) -> None:
    seed_portable_skills(tmp_path)
    expected = {
        "test-runner", "python-work", "structured-data", "notes",
        "document-processing", "media-processing",
    }
    config = CapabilitiesConfig(
        selection="filtered",
        ambient_types=["workspace", "text/query"],
        top_k=64,
    )
    loader = SkillsLoader(tmp_path, capabilities=config)
    workspace_names = {
        skill["name"] for skill in loader.list_skills(filter_unavailable=False)
        if skill["source"] == "workspace"
    }
    assert expected <= workspace_names

    builder = ContextBuilder(tmp_path, capabilities=config, inject_memory=False)
    selection = builder._run_selection(
        "Run Python tests and inspect JSON, CSV, PDF, documents, image, and audio files.",
        [
            Attachment(name="data.json", mime="application/json"),
            Attachment(name="table.csv", mime="text/csv"),
            Attachment(name="paper.pdf", mime="application/pdf"),
            Attachment(
                name="document.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
            Attachment(name="photo.png", mime="image/png"),
            Attachment(name="recording.wav", mime="audio/wav"),
        ],
    )
    assert selection.mode == "filtered"
    assert expected <= set(selection.survivors)
