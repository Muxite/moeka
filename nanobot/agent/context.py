"""Context builder for assembling agent prompts."""

from __future__ import annotations

import base64
import mimetypes
import platform
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from loguru import logger

from nanobot.agent.memory import MemoryStore
from nanobot.agent.skills import SkillsLoader
from nanobot.agent.tools import image_generation as image_generation_tools
from nanobot.agent.tools import mcp as mcp_tools
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.apps.cli import utils as cli_app_utils
from nanobot.bus.events import InboundMessage
from nanobot.runtime_context import (
    RUNTIME_CONTEXT_END,
    RUNTIME_CONTEXT_MESSAGE_META,
    RUNTIME_CONTEXT_TAG,
    RuntimeContextBlock,
    append_runtime_context,
)
from nanobot.utils.helpers import (
    detect_image_mime,
    load_bundled_template,
    truncate_text_to_tokens,
)
from nanobot.utils.prompt_templates import render_template

if TYPE_CHECKING:
    from nanobot.config.schema import VecConfig
    from nanobot.core.vec_store import VecStore


def session_extra(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return persisted kwargs for turn-attached capabilities."""
    return cli_app_utils.session_extra(metadata) | mcp_tools.session_extra(metadata)


async def connect_mcp(state: Any, tools: ToolRegistry) -> None:
    await mcp_tools.connect_missing_servers(state, tools)


async def close_mcp(state: Any) -> None:
    await mcp_tools.close_mcp_servers(state)


async def handle_runtime_control(state: Any, msg: InboundMessage, tools: ToolRegistry) -> bool:
    for handler in (
        image_generation_tools.handle_runtime_control,
        mcp_tools.handle_runtime_control,
    ):
        if await handler(state, msg, tools):
            return True
    return False


class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""

    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md"]
    _SKIPPABLE_DEFAULTS = {"AGENTS.md", "USER.md"}
    _RUNTIME_CONTEXT_TAG = RUNTIME_CONTEXT_TAG
    _MAX_RECENT_HISTORY = 50
    _MAX_HISTORY_TOKENS = 8_000  # hard cap on recent history section size (tokens)
    _RUNTIME_CONTEXT_END = RUNTIME_CONTEXT_END

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
        *,
        channel: str | None = None,
        query: str | None = None,
        session_summary: str | None = None,
        workspace: Path | None = None,
        include_memory_recent_history: bool = True,
        session_key: str | None = None,
        unified_session: bool = False,
    ) -> str:
        """Build the system prompt from identity, bootstrap files, memory, and skills."""
        root = workspace or self.workspace
        parts = [self._get_identity(channel=channel, workspace=root)]

        bootstrap = self._load_bootstrap_files(root)
        if bootstrap:
            parts.append(bootstrap)

        parts.append(self._behavioral_guidelines())
        parts.append(render_template("agent/tool_contract.md"))

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

        if include_memory_recent_history:
            history_section = self._build_history_section(
                query=query,
                session_key=session_key,
                unified_session=unified_session,
            )
            if history_section:
                parts.append(history_section)

        if session_summary:
            parts.append(f"[Archived Context Summary]\n\n{session_summary}")

        return "\n\n---\n\n".join(parts)

    def _build_history_section(
        self,
        query: str | None = None,
        *,
        session_key: str | None = None,
        unified_session: bool = False,
    ) -> str:
        """Build the Recent History section using hybrid recency + semantic retrieval."""
        vc = self.vec_config
        recent_k = vc.history_recent_k if vc else 15
        semantic_k = vc.history_semantic_k if vc else 10

        dream_cursor = self.memory.get_last_dream_cursor()
        all_entries = self.memory.read_recent_history_for_prompt(
            since_cursor=dream_cursor,
            session_key=session_key,
            unified_session=unified_session,
        )
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
        history_text = truncate_text_to_tokens(history_text, self._MAX_HISTORY_TOKENS)
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

    def _get_identity(self, channel: str | None = None, workspace: Path | None = None) -> str:
        """Get the core identity section."""
        root = workspace or self.workspace
        workspace_path = str(root.expanduser().resolve())
        agent_workspace_path = str(self.workspace.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"

        return render_template(
            "agent/identity.md",
            workspace_path=workspace_path,
            agent_workspace_path=agent_workspace_path,
            runtime=runtime,
            platform_policy=render_template("agent/platform_policy.md", system=system),
            channel=channel or "",
        )

    @staticmethod
    def _merge_message_content(left: Any, right: Any) -> str | list[dict[str, Any]]:
        if isinstance(left, str) and isinstance(right, str):
            if not left:
                return right
            if not right:
                return left
            return f"{left}\n\n{right}"

        def _to_blocks(value: Any) -> list[dict[str, Any]]:
            if isinstance(value, list):
                return [item if isinstance(item, dict) else {"type": "text", "text": str(item)} for item in value]
            if value is None:
                return []
            return [{"type": "text", "text": str(value)}]

        return _to_blocks(left) + _to_blocks(right)

    def _load_bootstrap_files(self, workspace: Path | None = None) -> str:
        """Load project instructions plus the agent's global profile files.

        moeka: an in-memory ``bootstrap_overrides`` entry shadows the on-disk file
        of the same name, and any override with no corresponding source is
        appended. That is the embedding-host surface behind
        ``MoekaCore.set_bootstrap()``; ``nanobot/core/core.py`` passes it through
        ``AgentLoop.from_config``, so removing it here would make ``MoekaCore``
        raise TypeError at runtime while the merge stayed clean.
        """
        parts = []
        project_root = workspace or self.workspace
        sources = [
            ("AGENTS.md", project_root),
            ("SOUL.md", self.workspace),
            ("USER.md", self.workspace),
        ]

        for filename, root in sources:
            # An in-memory override replaces the file entirely. Checked first so
            # a host can shadow a name that also exists on disk; the template
            # detection below is about *default* on-disk content and would be
            # wrong to apply to content the host supplied deliberately.
            override = self.bootstrap_overrides.get(filename)
            if override is not None:
                if override.strip():
                    parts.append(f"## {filename}\n\n{override}")
                continue

            file_path = root / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                if filename == "SOUL.md" and self._is_template_content(
                    content,
                    "legacy/SOUL.md",
                ):
                    content = load_bundled_template("SOUL.md") or content
                if not content.strip():
                    continue
                if filename in self._SKIPPABLE_DEFAULTS and self._is_template_content(
                    content, filename
                ):
                    continue
                parts.append(f"## {filename}\n\n{content}")

        # Overrides naming something that is not a bootstrap source at all.
        known = {filename for filename, _ in sources}
        for name, content in self.bootstrap_overrides.items():
            if name not in known:
                parts.append(f"## {name}\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    @staticmethod
    def _is_template_content(content: str, template_path: str) -> bool:
        """Check if *content* is identical to the bundled template (user hasn't customized it)."""
        tpl = load_bundled_template(template_path)
        if tpl is not None:
            return content.strip() == tpl.strip()
        return False

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        *,
        media: list[str] | None = None,
        channel: str | None = None,
        current_role: str = "user",
        session_summary: str | None = None,
        runtime_context_blocks: Sequence[RuntimeContextBlock] | None = None,
        workspace: Path | None = None,
        include_memory_recent_history: bool = True,
        session_key: str | None = None,
        unified_session: bool = False,
    ) -> list[dict[str, Any]]:
        """Build the complete message list for an LLM call."""
        root = workspace or self.workspace
        user_content = self._build_user_content(current_message, media)
        blocks = list(runtime_context_blocks or ()) if current_role == "user" else []
        merged, runtime_context_meta = append_runtime_context(user_content, blocks)
        messages = [
            {
                "role": "system",
                "content": self.build_system_prompt(
                    channel=channel,
                    query=current_message or None,
                    session_summary=session_summary,
                    workspace=root,
                    include_memory_recent_history=include_memory_recent_history,
                    session_key=session_key,
                    unified_session=unified_session,
                ),
            },
            *history,
        ]
        if messages[-1].get("role") == current_role:
            last = dict(messages[-1])
            last["content"] = self._merge_message_content(last.get("content"), merged)
            if current_role == "user" and runtime_context_meta is not None:
                internal_meta = dict(last.get("_meta") or {})
                internal_meta[RUNTIME_CONTEXT_MESSAGE_META] = runtime_context_meta
                last["_meta"] = internal_meta
            messages[-1] = last
            return messages
        current = {"role": current_role, "content": merged}
        if current_role == "user" and runtime_context_meta is not None:
            current["_meta"] = {RUNTIME_CONTEXT_MESSAGE_META: runtime_context_meta}
        messages.append(current)
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
