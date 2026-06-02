"""``MoekaCore`` — a reusable RAG/agentic "thinking core".

A small, stable facade over moeka's full agent engine for embedding in other
Python code. It wraps :class:`~nanobot.agent.loop.AgentLoop` (batteries-included:
memory, sessions, semantic retrieval) and adds two host-facing capabilities the
chat-bot runtime never exposed:

  * **Actions** — register a plain Python callable as a tool the agent can call.
  * **Documents** — ingest arbitrary text/files into a vector collection and
    retrieve over them (RAG for host knowledge, alongside the agent's own memory).

Usage::

    core = MoekaCore.create()                       # uses ~/.nanobot/config.json

    @core.action
    def get_weather(city: str) -> str:
        "Return the current weather for a city."
        return lookup(city)

    core.ingest("Project X ships on Friday.", source="notes")
    result = await core.run("What's the weather in Paris and when does X ship?")
    print(result.content, result.tools_used)
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from nanobot.agent.hook import AgentHook, SDKCaptureHook
from nanobot.agent.loop import AgentLoop
from nanobot.core.function_tool import FunctionTool
from nanobot.nanobot import RunResult


class MoekaCore:
    """Programmatic facade for moeka's RAG/agentic thinking core.

    Construct via :meth:`create`. The underlying :class:`AgentLoop` runs with an
    internal :class:`~nanobot.bus.queue.MessageBus`; its outbound queue is drained
    after each run so long-lived processes don't accumulate messages.
    """

    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop
        # Set by :meth:`create` when it allocated a throwaway workspace for an
        # in-memory config; ``None`` when the host owns the workspace.
        self._ephemeral_workspace: Path | None = None

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    # Default workspace sentinel — when an in-memory config still carries this,
    # the core has no instance dir of its own and falls back to an ephemeral one
    # instead of writing into the user's ``~/.nanobot``.
    _DEFAULT_WORKSPACE = "~/.nanobot"

    @classmethod
    def create(
        cls,
        *,
        config: Any | None = None,
        config_dict: dict[str, Any] | None = None,
        config_path: str | Path | None = None,
        workspace: str | Path | None = None,
        model: str | None = None,
        provider: Any | None = None,
    ) -> MoekaCore:
        """Build a core from moeka config — files optional.

        This is the adapter/router that turns *whatever the host has* into the
        pydantic :class:`~nanobot.config.schema.Config` the core actually needs,
        then hands off to :meth:`from_config`. Supply **at most one** config
        source (precedence top→bottom):

        Args:
            config: A pre-built :class:`Config` object (pure data; no disk read).
            config_dict: A plain ``dict`` (e.g. parsed JSON); validated into a
                :class:`Config` and env-var-resolved in memory — no file needed.
            config_path: Path to a ``config.json`` file to read.
            workspace: Override where memory/sessions/vec.db live. When omitted
                and the config carries no explicit workspace, an in-memory config
                gets an **ephemeral** temp dir (so embedding the core never
                pollutes ``~/.nanobot``); the file/default route keeps using the
                config's own workspace.
            model: Override the resolved model id.
            provider: Pre-built :class:`LLMProvider` to use instead of building one
                from config (lets a host fully control provider selection).
        """
        from nanobot.config.loader import config_from_sources

        cfg, from_file = config_from_sources(
            config=config, config_dict=config_dict, config_path=config_path,
        )

        # Resolve the workspace. An explicit arg always wins. Otherwise the
        # file/default route trusts the config's own workspace, while an
        # in-memory config with only the default sentinel gets an ephemeral dir.
        ws: str | Path | None = workspace
        ephemeral: Path | None = None
        if ws is None and not from_file:
            ws_str = cfg.agents.defaults.workspace
            if ws_str == cls._DEFAULT_WORKSPACE or "${" in ws_str:
                import tempfile

                ephemeral = Path(tempfile.mkdtemp(prefix="moeka-core-"))
                ws = ephemeral

        core = cls.from_config(cfg, workspace=ws, model=model, provider=provider)
        core._ephemeral_workspace = ephemeral
        return core

    @classmethod
    def from_config(
        cls,
        config: Any,
        *,
        workspace: str | Path | None = None,
        model: str | None = None,
        provider: Any | None = None,
    ) -> MoekaCore:
        """Build a core directly from an in-memory :class:`Config` (the data seam).

        Pure ``(Config, workspace) -> MoekaCore``: it does not read or discover any
        config file. ``workspace`` overrides ``config.agents.defaults.workspace``
        when given; otherwise the config's own workspace is used as-is.
        """
        if workspace is not None:
            config.agents.defaults.workspace = str(Path(workspace).expanduser().resolve())

        defaults = config.agents.defaults
        extra: dict[str, Any] = {
            "image_generation_provider_configs": {
                "openrouter": config.providers.openrouter,
                "aihubmix": config.providers.aihubmix,
            },
            "vec_config": defaults.vec,
            "vec_store": cls._build_vec_store(config),
        }
        if provider is not None:
            extra["provider"] = provider
        if model is not None:
            extra["model"] = model

        loop = AgentLoop.from_config(config, **extra)
        return cls(loop)

    @staticmethod
    def _build_vec_store(config: Any) -> Any | None:
        """Construct the semantic store when enabled (degrades gracefully).

        The product runtime never instantiates a VecStore, so the core wires it
        up here — at ``<workspace>/memory/vec.db`` — to make RAG over memory,
        history, and host documents actually work. Returns ``None`` when disabled;
        an unavailable store (``moeka[vec]`` missing) is harmless and inert.
        """
        vec_config = config.agents.defaults.vec
        if not getattr(vec_config, "enable", False):
            return None
        from nanobot.agent.vec_store import VecStore

        db_path = config.workspace_path / "memory" / "vec.db"
        return VecStore(db_path, model_name=vec_config.embedding_model)

    @classmethod
    def from_loop(cls, loop: AgentLoop) -> MoekaCore:
        """Wrap an already-constructed :class:`AgentLoop` (advanced use)."""
        return cls(loop)

    @property
    def loop(self) -> AgentLoop:
        """The wrapped :class:`AgentLoop`, for advanced configuration."""
        return self._loop

    @property
    def workspace(self) -> Path:
        """The resolved workspace directory backing this core's persistence."""
        return self._loop.workspace

    def cleanup(self) -> None:
        """Remove the ephemeral workspace, if :meth:`create` allocated one.

        No-op when the host supplied its own workspace (nothing to clean up).
        """
        ws = self._ephemeral_workspace
        if ws is None:
            return
        import shutil

        shutil.rmtree(ws, ignore_errors=True)
        self._ephemeral_workspace = None

    # ------------------------------------------------------------------
    # Actions — connect host code to the agent
    # ------------------------------------------------------------------

    def action(
        self,
        fn: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
        description: str | None = None,
        parameters: dict[str, Any] | None = None,
        read_only: bool = False,
    ) -> Any:
        """Register a callable as an agent tool. Usable bare or parameterized::

            @core.action
            def f(x: int) -> str: ...

            @core.action(name="lookup", read_only=True)
            def g(q: str) -> str: ...
        """

        def register(func: Callable[..., Any]) -> Callable[..., Any]:
            self.register_action(
                func,
                name=name,
                description=description,
                parameters=parameters,
                read_only=read_only,
            )
            return func

        return register if fn is None else register(fn)

    def register_action(
        self,
        fn: Callable[..., Any],
        *,
        name: str | None = None,
        description: str | None = None,
        parameters: dict[str, Any] | None = None,
        read_only: bool = False,
    ) -> str:
        """Imperative form of :meth:`action`. Returns the registered tool name."""
        tool = FunctionTool(
            fn,
            name=name,
            description=description,
            parameters=parameters,
            read_only=read_only,
        )
        self._loop.tools.register(tool)
        return tool.name

    def unregister_action(self, name: str) -> None:
        """Remove a previously registered action."""
        self._loop.tools.unregister(name)

    # ------------------------------------------------------------------
    # Documents — RAG over host-supplied knowledge
    # ------------------------------------------------------------------

    @property
    def vec_available(self) -> bool:
        """True when semantic retrieval is usable (``moeka[vec]`` installed)."""
        vs = self._loop.vec_store
        return bool(vs is not None and vs.available)

    def ingest(self, text_or_path: str | Path, *, source: str | None = None) -> int:
        """Ingest text or a document file into the host-document collection.

        ``text_or_path`` is treated as a file path if it points at an existing
        file, otherwise as raw text. Returns the number of chunks indexed (0 when
        ``moeka[vec]`` is not installed).
        """
        vs = self._loop.vec_store
        if vs is None or not vs.available:
            return 0

        text, src = self._resolve_ingest_input(text_or_path, source)
        if not text.strip():
            return 0
        return vs.add_documents(text, source=src)

    @staticmethod
    def _resolve_ingest_input(
        text_or_path: str | Path, source: str | None
    ) -> tuple[str, str | None]:
        from nanobot.utils.document import extract_text

        candidate = Path(text_or_path) if isinstance(text_or_path, (str, Path)) else None
        if candidate is not None and len(str(text_or_path)) < 4096 and candidate.exists() \
                and candidate.is_file():
            extracted = extract_text(candidate)
            if extracted and not extracted.startswith("[error"):
                return extracted, source or candidate.name
        return str(text_or_path), source

    def retrieve(self, query: str, *, k: int = 5) -> list[str]:
        """Return the top-k host-document chunks semantically closest to *query*."""
        vs = self._loop.vec_store
        if vs is None or not vs.available:
            return []
        return vs.search_documents(query, k=k)

    # ------------------------------------------------------------------
    # Run the thinking loop
    # ------------------------------------------------------------------

    async def run(
        self,
        message: str,
        *,
        session_key: str = "core:default",
        media: list[str] | None = None,
        hooks: list[AgentHook] | None = None,
    ) -> RunResult:
        """Run one agent turn (multi-step tool calling + RAG context) and return it.

        Different ``session_key`` values get independent conversation history.
        """
        capture = SDKCaptureHook()
        prev = self._loop._extra_hooks
        base_hooks = list(hooks) if hooks is not None else list(prev or [])
        self._loop._extra_hooks = [capture, *base_hooks]
        try:
            response = await self._loop.process_direct(
                message, session_key=session_key, media=media,
            )
        finally:
            self._loop._extra_hooks = prev
            self._drain_outbound()

        content = (response.content if response else None) or ""
        return RunResult(
            content=content,
            tools_used=capture.tools_used,
            messages=capture.messages,
        )

    async def think(self, message: str, **kwargs: Any) -> str:
        """Convenience wrapper around :meth:`run` returning just the text reply."""
        return (await self.run(message, **kwargs)).content

    def _drain_outbound(self) -> None:
        """Empty the internal bus outbound queue (nothing consumes it here)."""
        bus = getattr(self._loop, "bus", None)
        queue = getattr(bus, "outbound", None)
        if queue is None:
            return
        try:
            while not queue.empty():
                queue.get_nowait()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # One-shot completion (no loop, no tools)
    # ------------------------------------------------------------------

    @staticmethod
    async def complete(prompt: str, **kwargs: Any) -> str:
        """One-shot completion through moeka's provider layer (no agent loop).

        Thin delegate to :func:`nanobot.api.complete.acomplete`; accepts the same
        keyword arguments (``system``, ``images``, ``model``, ``preset``, ...).
        """
        from nanobot.api.complete import acomplete

        return await acomplete(prompt, **kwargs)
