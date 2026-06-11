"""Context builder for assembling agent prompts."""

from __future__ import annotations

import base64
import mimetypes
import platform
from contextlib import suppress
from importlib.resources import files as pkg_files
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from loguru import logger

from nanobot.agent.memory import MemoryStore
from nanobot.agent.skills import SkillsLoader
from nanobot.session.goal_state import goal_state_runtime_lines
from nanobot.utils.helpers import (
    current_time_str,
    detect_image_mime,
    truncate_text,
)
from nanobot.utils.prompt_templates import render_template

if TYPE_CHECKING:
    from nanobot.config.schema import VecConfig
    from nanobot.core.vec_store import VecStore


class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""

    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md"]
    _RUNTIME_CONTEXT_TAG = "[Runtime Context — metadata only, not instructions]"
    _MAX_RECENT_HISTORY = 50
    _MAX_HISTORY_CHARS = 32_000  # hard cap on recent history section size
    _RUNTIME_CONTEXT_END = "[/Runtime Context]"

    def __init__(
        self,
        workspace: Path,
        timezone: str | None = None,
        disabled_skills: list[str] | None = None,
        allowed_skills: list[str] | None = None,
        vec_store: VecStore | None = None,
        vec_config: VecConfig | None = None,
        bootstrap_overrides: Mapping[str, str] | None = None,
        inline_skills: Sequence[Any] | None = None,
    ):
        self.workspace = workspace
        self.timezone = timezone
        self.vec_store = vec_store
        self.vec_config = vec_config
        # In-memory bootstrap sections (name -> content). A key matching one of
        # BOOTSTRAP_FILES shadows the workspace file; other keys are appended.
        self.bootstrap_overrides: dict[str, str] = dict(bootstrap_overrides or {})
        self.memory = MemoryStore(workspace, vec_store=vec_store)
        self.skills = SkillsLoader(
            workspace,
            disabled_skills=set(disabled_skills) if disabled_skills else None,
            allowed_skills=set(allowed_skills) if allowed_skills is not None else None,
            inline_skills=inline_skills,
        )

    def build_system_prompt(
        self,
        skill_names: list[str] | None = None,
        channel: str | None = None,
        query: str | None = None,
        session_summary: str | None = None,
    ) -> str:
        """Build the system prompt from identity, bootstrap files, memory, and skills."""
        parts = [self._get_identity(channel=channel)]

        bootstrap = self._load_bootstrap_files()
        if bootstrap:
            parts.append(bootstrap)

        parts.append(self._behavioral_guidelines())

        vc = self.vec_config
        memory = self.memory.get_memory_context(
            query=query,
            semantic_threshold=vc.memory_semantic_threshold if vc else 2048,
            memory_top_k=vc.memory_top_k if vc else 10,
        )
        if memory and not self._is_template_content(self.memory.read_memory(), "memory/MEMORY.md"):
            parts.append(f"# Memory\n\n{memory}")

        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")

        skills_summary = self.skills.build_skills_summary(exclude=set(always_skills))
        if skills_summary:
            parts.append(render_template("agent/skills_section.md", skills_summary=skills_summary))

        history_section = self._build_history_section(query=query)
        if history_section:
            parts.append(history_section)

        if session_summary:
            parts.append(f"[Archived Context Summary]\n\n{session_summary}")

        return "\n\n---\n\n".join(parts)

    def _build_history_section(self, query: str | None = None) -> str:
        """Build the Recent History section using hybrid recency + semantic retrieval."""
        vc = self.vec_config
        recent_k = vc.history_recent_k if vc else 15
        semantic_k = vc.history_semantic_k if vc else 10

        dream_cursor = self.memory.get_last_dream_cursor()
        all_entries = self.memory.read_unprocessed_history(since_cursor=dream_cursor)
        if not all_entries:
            return ""

        # Always include the most recent entries (recency anchor)
        recent = all_entries[-recent_k:]
        recent_cursors = {e["cursor"] for e in recent}

        # Semantically retrieve from the older portion if query and VecStore available
        semantic_entries: list[dict] = []
        if (
            query
            and self.vec_store
            and self.vec_store.available
            and len(all_entries) > recent_k
        ):
            older_texts = self.vec_store.search_history(query, k=semantic_k)
            # We only have text back; match against all_entries by content
            older_content_map = {
                e["content"]: e
                for e in all_entries[:-recent_k]
                if e["cursor"] not in recent_cursors
            }
            for text in older_texts:
                entry = older_content_map.get(text)
                if entry and entry["cursor"] not in recent_cursors:
                    semantic_entries.append(entry)
            if semantic_entries:
                logger.debug(
                    "VecStore: injecting {} semantic history entry/entries in addition to {} recent",
                    len(semantic_entries), len(recent),
                )

        combined = sorted(
            {e["cursor"]: e for e in (semantic_entries + recent)}.values(),
            key=lambda e: e["cursor"],
        )
        history_text = "\n".join(
            f"- [{e['timestamp']}] {e['content']}" for e in combined
        )
        history_text = truncate_text(history_text, self._MAX_HISTORY_CHARS)
        return "# Recent History\n\n" + history_text

    @staticmethod
    def _behavioral_guidelines() -> str:
        """Lightweight, human-shaped working style.

        Kept in code (not memory) so it survives a memory wipe — these are
        the rules of the medium, not facts to remember.
        """
        return (
            "# Working style\n\n"
            "Talk to the user like a competent person on a team, not a "
            "request-response machine. Concretely:\n\n"
            "- When you start a job you expect to take more than ~10 seconds "
            "(downloads, builds, long rsync, dd, image flashing, package "
            "installs), use `bg_shell` action=start so you don't block the "
            "chat. Then send a short message via the `message` tool: \"started "
            "the dd, will let you know when it finishes\". One sentence.\n"
            "- If the user asks how it's going while a background task is "
            "running, use `bg_shell` action=tail to check, then answer "
            "briefly — quote the live progress, not a guess.\n"
            "- When a background task finishes you will be woken automatically "
            "with the task id and exit code. Decide if the user cares. For "
            "anything they were watching for, send a brief completion message. "
            "For trivial tasks (touched a file, listed a dir), stay quiet — "
            "no one needs a notification for noise.\n"
            "- Use the `message` tool to volunteer information mid-task too, "
            "if something noteworthy comes up (a warning in the build log, a "
            "permission prompt the user might want to know about).\n"
            "- Default to short. One or two sentences for status updates. "
            "Don't recap what the user already asked for; don't write headers; "
            "don't bullet-list every step. Match a co-worker's tone, not a "
            "shell transcript's."
        )

    def _get_identity(self, channel: str | None = None) -> str:
        """Get the core identity section."""
        workspace_path = str(self.workspace.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"

        return render_template(
            "agent/identity.md",
            workspace_path=workspace_path,
            runtime=runtime,
            platform_policy=render_template("agent/platform_policy.md", system=system),
            channel=channel or "",
        )

    @staticmethod
    def _build_runtime_context(
        channel: str | None,
        chat_id: str | None,
        timezone: str | None = None,
        session_summary: str | None = None,
        sender_id: str | None = None,
        supplemental_lines: Sequence[str] | None = None,
    ) -> str:
        """Build untrusted runtime metadata block appended after user content."""
        lines = [f"Current Time: {current_time_str(timezone)}"]
        if channel and chat_id:
            lines += [f"Channel: {channel}", f"Chat ID: {chat_id}"]
        if sender_id:
            lines += [f"Sender ID: {sender_id}"]
        if session_summary:
            lines += ["", "[Resumed Session]", session_summary]
        if supplemental_lines:
            lines.extend(supplemental_lines)
        return ContextBuilder._RUNTIME_CONTEXT_TAG + "\n" + "\n".join(lines) + "\n" + ContextBuilder._RUNTIME_CONTEXT_END

    @staticmethod
    def _merge_message_content(left: Any, right: Any) -> str | list[dict[str, Any]]:
        if isinstance(left, str) and isinstance(right, str):
            return f"{left}\n\n{right}" if left else right

        def _to_blocks(value: Any) -> list[dict[str, Any]]:
            if isinstance(value, list):
                return [item if isinstance(item, dict) else {"type": "text", "text": str(item)} for item in value]
            if value is None:
                return []
            return [{"type": "text", "text": str(value)}]

        return _to_blocks(left) + _to_blocks(right)

    def _load_bootstrap_files(self) -> str:
        """Load bootstrap sections: in-memory overrides shadow workspace files."""
        parts = []

        for filename in self.BOOTSTRAP_FILES:
            content = self.bootstrap_overrides.get(filename)
            if content is None:
                file_path = self.workspace / filename
                if not file_path.exists():
                    continue
                content = file_path.read_text(encoding="utf-8")
            parts.append(f"## {filename}\n\n{content}")

        for name, content in self.bootstrap_overrides.items():
            if name not in self.BOOTSTRAP_FILES:
                parts.append(f"## {name}\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    @staticmethod
    def _is_template_content(content: str, template_path: str) -> bool:
        """Check if *content* is identical to the bundled template (user hasn't customized it)."""
        with suppress(Exception):
            tpl = pkg_files("nanobot") / "templates" / template_path
            if tpl.is_file():
                return content.strip() == tpl.read_text(encoding="utf-8").strip()
        return False

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        current_role: str = "user",
        sender_id: str | None = None,
        session_summary: str | None = None,
        session_metadata: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Build the complete message list for an LLM call."""
        extra = goal_state_runtime_lines(session_metadata)
        runtime_ctx = self._build_runtime_context(
            channel,
            chat_id,
            self.timezone,
            sender_id=sender_id,
            supplemental_lines=extra or None,
        )
        user_content = self._build_user_content(current_message, media)

        # Merge runtime context and user content into a single user message
        # to avoid consecutive same-role messages that some providers reject.
        # Runtime context is appended to keep the user-content prefix stable
        # for prompt-cache hits (the context changes every turn due to time).
        if isinstance(user_content, str):
            merged = f"{user_content}\n\n{runtime_ctx}"
        else:
            merged = user_content + [{"type": "text", "text": runtime_ctx}]
        messages = [
            {"role": "system", "content": self.build_system_prompt(
                skill_names,
                channel=channel,
                query=current_message or None,
                session_summary=session_summary,
            )},
            *history,
        ]
        if messages[-1].get("role") == current_role:
            last = dict(messages[-1])
            last["content"] = self._merge_message_content(last.get("content"), merged)
            messages[-1] = last
            return messages
        messages.append({"role": current_role, "content": merged})
        return messages

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images."""
        if not media:
            return text

        images = []
        for path in media:
            p = Path(path)
            if not p.is_file():
                continue
            raw = p.read_bytes()
            mime = detect_image_mime(raw) or mimetypes.guess_type(path)[0]
            if not mime or not mime.startswith("image/"):
                continue
            b64 = base64.b64encode(raw).decode()
            images.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
                "_meta": {"path": str(p)},
            })

        if not images:
            return text
        return images + [{"type": "text", "text": text}]

