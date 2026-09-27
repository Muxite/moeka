"""Context builder for assembling agent prompts."""

from __future__ import annotations

import base64
import mimetypes
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence, cast

from loguru import logger

from nanobot.agent.memory import MemoryStore
from nanobot.agent.skills import SkillsLoader
from nanobot.agent.tools import image_generation as image_generation_tools
from nanobot.agent.tools import mcp as mcp_tools
from nanobot.agent.tools import sessions as session_tools
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.events import (
    INBOUND_META_RUNTIME_CONTROL,
    RUNTIME_CONTROL_SESSION_DISCARD,
    InboundMessage,
)
from nanobot.runtime_context import (
    RUNTIME_CONTEXT_MESSAGE_META,
    RuntimeContextBlock,
    append_runtime_context,
)
from nanobot.security.workspace_access import WorkspaceScopeResolver
from nanobot.session.keys import last_channel_from_metadata
from nanobot.session.manager import Session
from nanobot.session.summary import SessionSummary
from nanobot.utils.helpers import (
    detect_image_mime,
    load_bundled_template,
    truncate_text_to_tokens,
)
from nanobot.utils.prompt_templates import render_template

if TYPE_CHECKING:
    from nanobot.config.schema import VecConfig
    from nanobot.core.vec_store import VecStore
    from nanobot.kernel.env import CoreEnvironment
    from nanobot.kernel.variants import Variant


def session_extra(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return persisted kwargs for turn-attached capabilities."""
    return (
        mcp_tools.session_extra(metadata)
        | session_tools.session_extra(metadata)
    )


async def handle_runtime_control(state: Any, msg: InboundMessage, tools: ToolRegistry) -> bool:
    if msg.metadata.get(INBOUND_META_RUNTIME_CONTROL) == RUNTIME_CONTROL_SESSION_DISCARD:
        await state.discard_session(msg.session_key)
        return True
    return await image_generation_tools.handle_runtime_control(state, msg, tools)


@dataclass(frozen=True, slots=True)
class PersistedPromptContextResolver:
    """Restore prompt routing context when no inbound message is available."""

    workspace_scopes: WorkspaceScopeResolver
    unified_session: bool = False

    def __call__(self, session: Session) -> tuple[str | None, Path]:
        channel = session.key.split(":", 1)[0] if ":" in session.key else None
        if self.unified_session:
            route = last_channel_from_metadata(session.metadata)
            if route is not None:
                channel = route[0]
        scope = self.workspace_scopes.for_turn(
            channel=channel,
            message_metadata=None,
            session_metadata=session.metadata,
        )
        return channel, scope.project_path


@dataclass(frozen=True, slots=True)
class TranscriptInput:
    """Raw turn inputs from which ``ContextBuilder`` assembles a transcript."""

    history: list[dict[str, Any]]
    current_message: str | None
    media: Sequence[str] | None = None
    current_role: str = "user"
    session_summary: SessionSummary | None = None
    runtime_context_blocks: Sequence[RuntimeContextBlock] | None = None

    @property
    def message_count(self) -> int:
        """Number of boundary-preserving messages in the assembled transcript."""
        return 1 + len(self.history) + (self.current_message is not None)


class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""

    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md"]
    _SKIPPABLE_DEFAULTS = {"AGENTS.md", "USER.md"}
    # moeka: hard cap on the "# Recent History" semantic-memory section (tokens).
    _MAX_HISTORY_TOKENS = 8_000

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
        env: CoreEnvironment | None = None,
        variant: Variant | None = None,
    ):
        self.workspace = workspace
        # Variant (Task 8): template roots, bundled-skills dir and bootstrap entries.
        # ``None`` / ``Variant()`` = the built-ins, byte for byte.
        self.variant = variant
        self._template_roots = variant.template_roots if variant is not None else ()
        self.timezone = timezone
        self.vec_store = vec_store
        self.vec_config = vec_config
        # In-memory bootstrap sections (name -> content). A key matching one of
        # BOOTSTRAP_FILES shadows the workspace file; other keys are appended. The
        # variant's entries come first; an explicit override of the same name wins.
        self.bootstrap_overrides: dict[str, str] = {
            **(variant.bootstrap if variant is not None else {}),
            **(bootstrap_overrides or {}),
        }
        # Host env: ``skill.listed`` goes to ``env.trace`` (none without an env).
        self._env = env
        self.memory = MemoryStore(workspace, vec_store=vec_store, env=env)
        self.skills = SkillsLoader(
            workspace,
            builtin_skills_dir=variant.builtin_skills_dir if variant is not None else None,
            disabled_skills=set(disabled_skills) if disabled_skills else None,
            allowed_skills=set(allowed_skills) if allowed_skills is not None else None,
            inline_skills=inline_skills,
            env=env,
        )

    def build_system_prompt(
        self,
        *,
        channel: str | None = None,
        query: str | None = None,
        session_summary: SessionSummary | None = None,
        workspace: Path | None = None,
        include_memory: bool = True,
        include_memory_recent_history: bool = True,
        session_key: str | None = None,
        unified_session: bool = False,
        trace: bool = True,
    ) -> str:
        """Build the system prompt from identity, bootstrap files, memory, and skills.

        ``trace=False`` skips the ``skill.listed`` event (``variants.fingerprint``
        renders the prompt without it being a turn).
        """
        root = workspace or self.workspace
        parts = [self._get_identity(channel=channel, workspace=root)]

        bootstrap = self._load_bootstrap_files(root)
        if bootstrap:
            parts.append(bootstrap)

        parts.append(self._behavioral_guidelines())
        roots = getattr(self, "_template_roots", ())
        parts.append(render_template("agent/tool_contract.md", roots=roots))

        project_path = root.expanduser().resolve()
        if project_path != self.workspace.expanduser().resolve():
            parts.append(
                "# Current Project\n\n"
                f"Working directory: {project_path}\n"
                "Use it as the default root for project files and relative tool paths."
            )

        if include_memory:
            vc = self.vec_config
            raw_memory = self.memory.read_memory()
            memory = self.memory.get_memory_context(
                query=query,
                semantic_threshold=vc.memory_semantic_threshold if vc else 2048,
                memory_top_k=vc.memory_top_k if vc else 10,
                long_term=raw_memory,
            )
            if memory and not self._is_template_content(raw_memory, "memory/MEMORY.md"):
                parts.append(f"# Memory\n\n## Long-term Memory\n{memory}")

        active_skills = self.skills.get_always_skills()
        active_content = ""
        if active_skills:
            active_content = self.skills.load_skills_for_context(active_skills)
            if active_content:
                parts.append(f"# Active Skills\n\n{active_content}")

        listed: list[str] = []
        skills_summary = self.skills.build_skills_summary(
            exclude=set(active_skills),
            workspace=root,
            listed=listed,
        )
        if skills_summary:
            parts.append(render_template(
                "agent/skills_section.md", roots=roots,
                skills_summary=skills_summary,
            ))
        if trace:
            self._trace_skills_listed(active_skills if active_content else [], listed)

        if include_memory_recent_history:
            history_section = self._build_history_section(
                query=query,
                session_key=session_key,
                unified_session=unified_session,
                session_summary=session_summary,
            )
            if history_section:
                parts.append(history_section)

        if session_summary and session_summary["text"] != "(nothing)":
            parts.append(
                "[Archived Context Summary]\n\n"
                f"Previous conversation summary (last active {session_summary['last_active']}):\n"
                f"{session_summary['text']}"
            )

        return "\n\n---\n\n".join(parts)

    def _trace_skills_listed(self, active: list[str], listed: list[str]) -> None:
        """Emit ``skill.listed`` (skills rendered into the prompt) to the env's trace."""
        env = getattr(self, "_env", None)
        if env is None or not (active or listed):
            return
        from nanobot.kernel.trace import safe_emit

        safe_emit(env.trace, {
            "event": "skill.listed", "skills": [*active, *listed], "active": list(active),
        })

    def _build_history_section(
        self,
        query: str | None = None,
        *,
        session_key: str | None = None,
        unified_session: bool = False,
        session_summary: SessionSummary | None = None,
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
        combined = self._without_duplicate_session_summary(
            combined,
            session_key=session_key,
            session_summary=session_summary,
        )
        if not combined:
            return ""
        history_text = "\n".join(
            f"- [{e['timestamp']}] {e['content']}" for e in combined
        )
        history_text = truncate_text_to_tokens(history_text, self._MAX_HISTORY_TOKENS)
        return "# Recent History\n\n" + history_text

    @staticmethod
    def _without_duplicate_session_summary(
        entries: list[dict[str, Any]],
        *,
        session_key: str | None,
        session_summary: SessionSummary | None,
    ) -> list[dict[str, Any]]:
        """Drop the history entry already represented by the session summary."""
        if not session_summary:
            return entries
        for index in range(len(entries) - 1, -1, -1):
            entry = entries[index]
            if (
                entry.get("session_key") == session_key
                and entry.get("content") == session_summary["text"]
            ):
                return [*entries[:index], *entries[index + 1:]]
        return entries

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
            "installs), run it with `exec` and a `yield_time_ms` so you don't "
            "block the chat; if it is still running, exec returns a "
            "`session_id`. Then say so in one sentence: \"started the dd, "
            "will check on it\".\n"
            "- If the user asks how it's going, poll the session with "
            "`exec_session`, then answer briefly — quote the live progress, "
            "not a guess.\n"
            "- Nothing wakes you when a session finishes; check with "
            "`exec_session` when it matters. Decide if the user cares. For "
            "anything they were watching for, send a brief completion message. "
            "For trivial tasks (touched a file, listed a dir), stay quiet — "
            "no one needs a notification for noise.\n"
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

        roots = getattr(self, "_template_roots", ())
        return render_template(
            "agent/identity.md",
            roots=roots,
            workspace_path=workspace_path,
            agent_workspace_path=agent_workspace_path,
            runtime=runtime,
            platform_policy=render_template(
                "agent/platform_policy.md", roots=roots, system=system,
            ),
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
                return [
                    cast(dict[str, Any], item)
                    if isinstance(item, dict)
                    else {"type": "text", "text": str(item)}
                    for item in cast(list[Any], value)
                ]
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
        parts: list[str] = []
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
        current_message: str | None,
        *,
        media: list[str] | None = None,
        channel: str | None = None,
        current_role: str = "user",
        session_summary: SessionSummary | None = None,
        runtime_context_blocks: Sequence[RuntimeContextBlock] | None = None,
        workspace: Path | None = None,
        include_memory: bool = True,
        include_memory_recent_history: bool = True,
        session_key: str | None = None,
        unified_session: bool = False,
    ) -> list[dict[str, Any]]:
        """Compatibility wrapper for callers that need merged adjacent roles."""
        messages = self.build_transcript(
            TranscriptInput(
                history=history,
                current_message=current_message,
                media=media,
                current_role=current_role,
                session_summary=session_summary,
                runtime_context_blocks=runtime_context_blocks,
            ),
            channel=channel,
            workspace=workspace,
            include_memory=include_memory,
            include_memory_recent_history=include_memory_recent_history,
            session_key=session_key,
            unified_session=unified_session,
        )
        if current_message is None:
            return messages
        current = messages[-1]
        if len(messages) < 2 or messages[-2].get("role") != current.get("role"):
            return messages

        merged = dict(messages[-2])
        merged["content"] = self._merge_message_content(
            merged.get("content"),
            current.get("content"),
        )
        current_meta = current.get("_meta")
        if current.get("role") == "user" and isinstance(current_meta, dict):
            internal_meta = dict(merged.get("_meta") or {})
            internal_meta.update(cast(dict[str, Any], current_meta))
            merged["_meta"] = internal_meta
        return [*messages[:-2], merged]

    def build_transcript(
        self,
        transcript: TranscriptInput,
        *,
        channel: str | None = None,
        workspace: Path | None = None,
        include_memory: bool = True,
        include_memory_recent_history: bool = True,
        session_key: str | None = None,
        unified_session: bool = False,
    ) -> list[dict[str, Any]]:
        """Build a model transcript while preserving the fresh-turn boundary."""
        root = workspace or self.workspace
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": self.build_system_prompt(
                    channel=channel,
                    query=transcript.current_message or None,
                    session_summary=transcript.session_summary,
                    workspace=root,
                    include_memory=include_memory,
                    include_memory_recent_history=include_memory_recent_history,
                    session_key=session_key,
                    unified_session=unified_session,
                ),
            },
            *transcript.history,
        ]
        if transcript.current_message is None:
            return messages

        current = self.build_current_message(
            transcript.current_message,
            media=list(transcript.media) if transcript.media else None,
            current_role=transcript.current_role,
            runtime_context_blocks=transcript.runtime_context_blocks,
        )
        messages.append(current)
        return messages

    def build_current_message(
        self,
        current_message: str,
        *,
        media: list[str] | None = None,
        current_role: str = "user",
        runtime_context_blocks: Sequence[RuntimeContextBlock] | None = None,
    ) -> dict[str, Any]:
        """Build only the fresh turn message without merging it into history."""
        content = self.build_user_content(current_message, image_paths=media)
        blocks: list[RuntimeContextBlock] = []
        if current_role == "user":
            blocks.extend(runtime_context_blocks or ())
            skill_context = self.skills.build_explicit_skill_runtime_context(current_message)
            if skill_context is not None and skill_context not in blocks:
                blocks.append(skill_context)
        merged, runtime_context_meta = append_runtime_context(content, blocks)
        current: dict[str, Any] = {"role": current_role, "content": merged}
        if current_role == "user" and runtime_context_meta is not None:
            current["_meta"] = {
                RUNTIME_CONTEXT_MESSAGE_META: runtime_context_meta,
            }
        return current

    def build_user_content(
        self,
        text: str,
        image_paths: list[str] | None,
    ) -> str | list[dict[str, Any]]:
        """Build user message content from prefiltered image paths."""
        if not image_paths:
            return text

        image_blocks: list[dict[str, Any]] = []
        for path in image_paths:
            p = Path(path)
            if not p.is_file():
                continue
            raw = p.read_bytes()
            # Re-detect from the bytes used for the request: the file may have
            # changed since attachment routing, and the data URL needs its MIME.
            mime = detect_image_mime(raw) or mimetypes.guess_type(path)[0]
            if not mime or not mime.startswith("image/"):
                continue
            b64 = base64.b64encode(raw).decode()
            image_blocks.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
                "_meta": {"path": str(p)},
            })

        if not image_blocks:
            return text
        return image_blocks + [{"type": "text", "text": text}]
