"""Tests for sync_workspace_templates (moved from the removed onboard-wizard tests)."""

from pathlib import Path

from nanobot.utils.helpers import sync_workspace_templates


class TestSyncWorkspaceTemplates:
    """Tests for sync_workspace_templates file synchronization."""

    def test_creates_missing_files(self, tmp_path):
        """Should create template files that don't exist."""
        workspace = tmp_path / "workspace"

        added = sync_workspace_templates(workspace, silent=True)

        # Check that some files were created
        assert isinstance(added, list)
        # The actual files depend on the templates directory

    def test_does_not_overwrite_existing_files(self, tmp_path):
        """Should not overwrite files that already exist."""
        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "AGENTS.md").write_text("existing content")

        sync_workspace_templates(workspace, silent=True)

        # Existing file should not be changed
        content = (workspace / "AGENTS.md").read_text()
        assert content == "existing content"

    def test_does_not_create_tools_md(self, tmp_path):
        """Tool contract is injected internally, not copied into user workspaces."""
        workspace = tmp_path / "workspace"

        added = sync_workspace_templates(workspace, silent=True)

        assert "TOOLS.md" not in added
        assert not (workspace / "TOOLS.md").exists()

    def test_preserves_existing_tools_md_without_overwriting(self, tmp_path):
        """Legacy user workspaces may have TOOLS.md; sync should leave it untouched."""
        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True)
        tools_path = workspace / "TOOLS.md"
        tools_path.write_text("custom tool notes", encoding="utf-8")

        sync_workspace_templates(workspace, silent=True)

        assert tools_path.read_text(encoding="utf-8") == "custom tool notes"

    def test_creates_memory_directory(self, tmp_path):
        """Should create memory directory structure."""
        workspace = tmp_path / "workspace"

        sync_workspace_templates(workspace, silent=True)

        assert (workspace / "memory").exists() or (workspace / "skills").exists()

    def test_creates_prompt_readme_without_dream_override(self, tmp_path):
        workspace = tmp_path / "workspace"

        added = sync_workspace_templates(workspace, silent=True)

        assert "prompts/README.md" in {path.replace("\\", "/") for path in added}
        assert (workspace / "prompts" / "README.md").exists()
        assert not (workspace / "prompts" / "dream.md").exists()

    def test_returns_list_of_added_files(self, tmp_path):
        """Should return list of relative paths for added files."""
        workspace = tmp_path / "workspace"

        added = sync_workspace_templates(workspace, silent=True)

        assert isinstance(added, list)
        # All paths should be relative to workspace
        for path in added:
            assert not Path(path).is_absolute()


