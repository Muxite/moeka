"""Skills loader for agent capabilities."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import yaml

from nanobot.runtime_context import RuntimeContextBlock

if TYPE_CHECKING:
    from nanobot.kernel.env import CoreEnvironment

# Default builtin skills directory (relative to this file)
BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills"

# Opening ---, YAML body (group 1), closing --- on its own line; supports CRLF.
_STRIP_SKILL_FRONTMATTER = re.compile(
    r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?",
    re.DOTALL,
)
_SKILL_NAME = re.compile(r"^(?!.*--)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_SKILL_REFERENCE = re.compile(r"(?<![\w$])\$([A-Za-z0-9_-]+)")


def parse_skill_metadata(content: str) -> dict[str, object] | None:
    """Parse a skill document's YAML frontmatter."""
    if not (match := _STRIP_SKILL_FRONTMATTER.match(content)):
        return None
    try:
        parsed = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, dict):
        return None
    return {str(key): value for key, value in cast(dict[object, object], parsed).items()}


def valid_skill_metadata(metadata: dict[str, object], name: str) -> bool:
    """Return whether metadata satisfies the Agent Skills identity contract."""
    description = metadata.get("description")
    return (
        metadata.get("name") == name
        and len(name) <= 64
        and _SKILL_NAME.fullmatch(name) is not None
        and isinstance(description, str)
        and 1 <= len(description.strip()) <= 1024
    )


class SkillsLoader:
    """
    Loader for agent skills.

    Skills are markdown files (SKILL.md) that teach the agent how to use
    specific tools or perform certain tasks.
    """

    def __init__(
        self,
        workspace: Path,
        builtin_skills_dir: Path | None = None,
        disabled_skills: set[str] | None = None,
        allowed_skills: set[str] | None = None,
        inline_skills: list | None = None,
        env: CoreEnvironment | None = None,
    ):
        self.workspace = workspace
        # Host env: ``requires.env`` presence is ``env.credentials.resolve(name,
        # scope="skills")``; ``None`` keeps the legacy process-env check.
        self._env = env
        self.workspace_skills = workspace / "skills"
        self.builtin_skills = builtin_skills_dir or BUILTIN_SKILLS_DIR
        self.disabled_skills = disabled_skills or set()
        # None = all skills allowed; a set restricts loading to those names.
        # Inline skills are exempt: the host registered them explicitly.
        self.allowed_skills = allowed_skills
        # In-memory skill definitions (objects or dicts with name/content/
        # description/metadata), keyed by name. Shadow workspace and builtin
        # skills of the same name.
        self.inline_skills: dict[str, object] = {}
        for skill in inline_skills or []:
            name = self._inline_field(skill, "name")
            if name:
                self.inline_skills[str(name)] = skill

    @staticmethod
    def _inline_field(skill: object, key: str, default=None):
        if isinstance(skill, dict):
            return skill.get(key, default)
        return getattr(skill, key, default)

    def _skill_aliases(self) -> dict[str, str]:
        """Return compatibility aliases owned by installed CLI Apps (gateway host).

        CLI Apps are a host feature that resolves its state from ambient locations,
        so a strict kernel-native env never consults them (``{}``).
        """
        if self._env is not None and getattr(self._env, "strict", False):
            return {}
        from nanobot.apps.cli import CliAppManager

        try:
            return CliAppManager(workspace=self.workspace).installed_skill_aliases()
        except OSError:
            return {}

    def _skill_entries_from_dir(self, base: Path, source: str, *, skip_names: set[str] | None = None) -> list[dict[str, str]]:
        if not base.exists():
            return []
        entries: list[dict[str, str]] = []
        for skill_dir in base.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue
            name = skill_dir.name
            if skip_names is not None and name in skip_names:
                continue
            entries.append({"name": name, "path": str(skill_file), "source": source})
        return entries

    def list_skills(self, filter_unavailable: bool = True) -> list[dict[str, str]]:
        """
        List all available skills.

        Args:
            filter_unavailable: If True, filter out skills with unmet requirements.

        Returns:
            List of skill info dicts with 'name', 'path', 'source'.
        """
        from nanobot.agent.plugins import enabled_agent_plugin_skills, plugin_data_root

        inline_names = set(self.inline_skills)
        skills = self._skill_entries_from_dir(
            self.workspace_skills, "workspace", skip_names=inline_names
        )
        seen_names = {entry["name"] for entry in skills} | inline_names
        plugin_skills = enabled_agent_plugin_skills(
            self.workspace, data_root=plugin_data_root(self._env)
        )
        for name, path in plugin_skills:
            if name in seen_names:
                continue
            skills.append(
                {
                    "name": name,
                    "path": str(path),
                    "source": "plugin",
                }
            )
            seen_names.add(name)
        if self.builtin_skills and self.builtin_skills.exists():
            skills.extend(
                self._skill_entries_from_dir(self.builtin_skills, "builtin", skip_names=seen_names)
            )

        if self.allowed_skills is not None:
            skills = [s for s in skills if s["name"] in self.allowed_skills]

        skills = [
            {"name": name, "path": f"inline:{name}", "source": "inline"}
            for name in self.inline_skills
        ] + skills

        if self.disabled_skills:
            disabled = set(self.disabled_skills)
            for legacy, canonical in self._skill_aliases().items():
                if legacy in disabled or canonical in disabled:
                    disabled.update((legacy, canonical))
            skills = [s for s in skills if s["name"] not in disabled]

        if filter_unavailable:
            return [skill for skill in skills if self._check_requirements(self._get_skill_meta(skill["name"]))]
        return skills

    def load_skill(self, name: str) -> str | None:
        """
        Load a skill by name.

        Args:
            name: Skill name (directory name).

        Returns:
            Skill content or None if not found.
        """
        inline = self.inline_skills.get(name)
        if inline is not None:
            return str(self._inline_field(inline, "content", "") or "")
        skills = self.list_skills(filter_unavailable=False)
        available = {skill["name"] for skill in skills}
        resolved = name if name in available else self._skill_aliases().get(name, name)
        entry = next((skill for skill in skills if skill["name"] == resolved), None)
        if entry is None:
            return None
        if entry["source"] == "inline":
            inline = self.inline_skills.get(resolved)
            return str(self._inline_field(inline, "content", "") or "") if inline is not None else None
        return Path(entry["path"]).read_text(encoding="utf-8")

    def load_skills_for_context(self, skill_names: list[str]) -> str:
        """
        Load specific skills for inclusion in agent context.

        Args:
            skill_names: List of skill names to load.

        Returns:
            Formatted skills content.
        """
        parts = [
            f"### Skill: {name}\n\n{self._strip_frontmatter(markdown)}"
            for name in skill_names
            if (markdown := self.load_skill(name))
        ]
        return "\n\n---\n\n".join(parts)

    def get_explicitly_invoked_skills(self, text: str) -> list[str]:
        """Resolve ``$skill-name`` references to enabled, available skills."""
        if not text:
            return []
        available = {
            entry["name"]
            for entry in self.list_skills(filter_unavailable=True)
        }
        aliases = self._skill_aliases()
        invoked: list[str] = []
        for match in _SKILL_REFERENCE.finditer(text):
            requested = match.group(1)
            name = requested if requested in available else aliases.get(requested, requested)
            if name in available and name not in invoked:
                invoked.append(name)
        return invoked

    def build_explicit_skill_runtime_context(
        self,
        text: str,
    ) -> RuntimeContextBlock | None:
        """Load non-always skills explicitly invoked by the current message."""
        skill_names = self.get_explicitly_invoked_skills(text)
        if not skill_names:
            return None
        always_active = set(self.get_always_skills())
        skill_names = [name for name in skill_names if name not in always_active]
        content = self.load_skills_for_context(skill_names)
        if not content:
            return None
        return RuntimeContextBlock(
            source="explicit_skills",
            content=(
                "[Active Skills — instructions for this user turn]\n"
                f"{content}\n"
                "[/Active Skills]"
            ),
        )

    def build_skills_summary(
        self,
        exclude: set[str] | None = None,
        *,
        workspace: Path | None = None,
        listed: list[str] | None = None,
    ) -> str:
        """
        Build a summary of all skills (name, description, path, availability).

        This is used for progressive loading - the agent can read the full
        skill content using read_file when needed.

        Args:
            exclude: Set of skill names to omit from the summary.
            workspace: Effective project workspace used to choose safe display paths.
            listed: When given, the names of the skills rendered are appended to it.

        Returns:
            Markdown-formatted skills summary.
        """
        all_skills = self.list_skills(filter_unavailable=False)
        if not all_skills:
            return ""

        agent_workspace = self.workspace.expanduser().resolve()
        project_workspace = (workspace or self.workspace).expanduser().resolve()
        use_relative_roots = project_workspace == agent_workspace
        sections: list[str] = []
        groups = (
            # moeka: upstream groups only workspace/builtin, which silently drops
            # inline skills from the summary — an embedding host's registered
            # skills would become invisible to the agent. root=None marks the
            # group as having no on-disk path.
            ("Inline skills", "inline", None),
            ("Workspace skills", "workspace", self.workspace_skills),
            ("Agent Plugin skills", "plugin", self.workspace / "plugins"),
            ("Built-in skills", "builtin", self.builtin_skills),
        )
        for label, source, root in groups:
            entries = [
                entry
                for entry in all_skills
                if entry["source"] == source and (not exclude or entry["name"] not in exclude)
            ]
            if not entries:
                continue

            if root is None:
                lines = [f"### {label}"]
            else:
                resolved_root = root.expanduser().resolve()
                if use_relative_roots:
                    display_root = Path("plugins" if source == "plugin" else "skills")
                else:
                    display_root = resolved_root
                lines = [f"### {label} (`{display_root}`)"]
            for entry in entries:
                skill_name = entry["name"]
                meta = self._get_skill_meta(skill_name)
                available = self._check_requirements(meta)
                desc = self.get_skill_description(skill_name)
                suffix = ""
                if not available:
                    missing = self._get_missing_requirements(meta)
                    suffix = f" (unavailable: {missing})" if missing else " (unavailable)"
                # moeka: inline skills exist only in memory — there is no file an
                # agent could read_file, and their synthetic "inline:<name>" path
                # is not under any root, so relative_to() would raise ValueError.
                path_hint = "" if root is None else (
                    f"  `{Path(entry['path']).relative_to(root).as_posix()}`"
                )
                lines.append(f"- **{skill_name}** — {desc}{suffix}{path_hint}")
                if listed is not None:
                    listed.append(skill_name)
            sections.append("\n".join(lines))
        return "\n\n".join(sections)

    @staticmethod
    def _requirement_lists(skill_meta: dict[str, Any]) -> tuple[list[str], list[str]]:
        """Return (bins, env) lists from skill metadata, tolerating null/wrong shapes."""
        requires = cast(dict[str, Any], skill_meta.get("requires") or {})
        if not isinstance(skill_meta.get("requires") or {}, dict):
            return [], []
        bins_raw: object = requires.get("bins") or []
        env_raw: object = requires.get("env") or []
        bins = [value for value in cast(list[object], bins_raw) if isinstance(value, str) and value.strip()] if isinstance(bins_raw, list) else []
        env = [value for value in cast(list[object], env_raw) if isinstance(value, str) and value.strip()] if isinstance(env_raw, list) else []
        return bins, env

    def _get_missing_requirements(self, skill_meta: dict[str, Any]) -> str:
        """Get a description of missing requirements."""
        required_bins, required_env_vars = self._requirement_lists(skill_meta)
        return ", ".join(
            [f"CLI: {command_name}" for command_name in required_bins if not shutil.which(command_name)]
            + [f"ENV: {env_name}" for env_name in required_env_vars if not self._env_present(env_name)]
        )

    def get_skill_availability(self, name: str) -> tuple[bool, str]:
        """Return whether a skill can run and why not when it cannot."""
        meta = self._get_skill_meta(name)
        available = self._check_requirements(meta)
        return available, "" if available else self._get_missing_requirements(meta)

    def get_skill_requirements(self, name: str) -> dict[str, list[str]]:
        """Return explicit command/env requirements and currently missing entries."""
        bins, env = self._requirement_lists(self._get_skill_meta(name))
        return {
            "bins": bins,
            "env": env,
            "missing_bins": [value for value in bins if not shutil.which(value)],
            "missing_env": [value for value in env if not self._env_present(value)],
        }

    def get_skill_description(self, name: str) -> str:
        """Get the description of a skill from its frontmatter."""
        meta = self.get_skill_metadata(name)
        description = meta.get("description") if meta else None
        if isinstance(description, str) and description:
            return description
        return name  # Fallback to skill name

    def _strip_frontmatter(self, content: str) -> str:
        """Remove YAML frontmatter from markdown content."""
        if not content.startswith("---"):
            return content
        match = _STRIP_SKILL_FRONTMATTER.match(content)
        if match:
            return content[match.end():].strip()
        return content

    def _parse_nanobot_metadata(self, raw: object) -> dict[str, Any]:
        """Extract nanobot/openclaw metadata from a frontmatter field.

        ``raw`` may be a dict (already parsed by yaml.safe_load) or a JSON str.
        """
        if isinstance(raw, dict):
            data = cast(dict[str, Any], raw)
        elif isinstance(raw, str):
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return {}
        else:
            return {}
        if not isinstance(data, dict):
            return {}
        data_object = cast(dict[str, Any], data)
        payload = data_object.get("nanobot", data_object.get("openclaw", {}))
        return cast(dict[str, Any], payload) if isinstance(payload, dict) else {}

    def _check_requirements(self, skill_meta: dict[str, Any]) -> bool:
        """Check if skill requirements are met (bins, env vars)."""
        required_bins, required_env_vars = self._requirement_lists(skill_meta)
        return all(shutil.which(cmd) for cmd in required_bins) and all(
            self._env_present(var) for var in required_env_vars
        )

    def _env_present(self, name: str) -> bool:
        """Whether a skill's required env name is available (a non-empty value)."""
        if self._env is not None:
            return bool(self._env.credentials.resolve(name, scope="skills"))
        from nanobot.kernel.legacy import ambient_env_var

        return bool(ambient_env_var(name))

    def _get_skill_meta(self, name: str) -> dict[str, Any]:
        """Get nanobot metadata for a skill (cached in frontmatter)."""
        raw_meta = self.get_skill_metadata(name) or {}
        return self._parse_nanobot_metadata(raw_meta.get("metadata"))

    def get_always_skills(self) -> list[str]:
        """Get skills marked as always=true that meet requirements."""
        return [
            entry["name"]
            for entry in self.list_skills(filter_unavailable=True)
            if (meta := self.get_skill_metadata(entry["name"]) or {})
            and (
                self._parse_nanobot_metadata(meta.get("metadata")).get("always")
                or meta.get("always")
            )
        ]

    def get_skill_metadata(self, name: str) -> dict[str, object] | None:
        """
        Get metadata from a skill's frontmatter.

        Args:
            name: Skill name.

        Returns:
            Metadata dict or None.
        """
        inline = self.inline_skills.get(name)
        if inline is not None:
            meta = dict(self._inline_field(inline, "metadata", None) or {})
            desc = self._inline_field(inline, "description", "") or name
            # Shape mirrors SKILL.md frontmatter: top-level fields plus the
            # nanobot payload under "metadata" (for _get_skill_meta /
            # _check_requirements / get_always_skills).
            return {"description": desc, **meta, "metadata": {"nanobot": meta}}
        return parse_skill_metadata(self.load_skill(name) or "")
