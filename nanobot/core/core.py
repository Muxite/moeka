"""``MoekaCore`` — a reusable RAG/agentic "thinking core".

A small, stable facade over moeka's full agent engine for embedding in other
Python code. It wraps :class:`~nanobot.agent.loop.AgentLoop` (batteries-included:
memory, sessions, semantic retrieval) and adds two host-facing capabilities the
chat-bot runtime never exposed:

  * **Actions** — register a plain Python callable as a tool the agent can call.
  * **Documents** — ingest arbitrary text/files into a vector collection and
    retrieve over them (RAG for host knowledge, alongside the agent's own memory).
  * **Epistemic stores** (P5, design I3) — ``kernel.facts`` (values with provenance),
    ``kernel.artifacts`` (typed artifacts whose committed leaves cite facts),
    ``kernel.propose`` and ``kernel.answer`` (commit a user's answer to a
    clarification question). Built lazily under ``env.paths.state_dir``.

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

import importlib
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot._deprecation import warn_deprecated
from nanobot.agent.hook import AgentHook, SDKCaptureHook
from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.core.function_tool import FunctionTool
from nanobot.core.vec import RetrievedChunk
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
        # Set by :meth:`create` when a named profile was applied.
        self.profile: Any | None = None
        self.profile_name: str | None = None
        # P5 epistemic stores (I3): built lazily from ``self.env`` on first use, or
        # injected by the host. ``_owns_*`` marks the ones :meth:`cleanup` closes.
        self._facts: Any | None = None
        self._artifacts: Any | None = None
        self._owns_facts = False
        self._owns_artifacts = False

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    # Default workspace sentinel — when an in-memory config still carries this,
    # the core has no instance dir of its own and falls back to an ephemeral one
    # instead of writing into the user's ``~/.nanobot``.
    _DEFAULT_WORKSPACE = "~/.nanobot"

    @classmethod
    def _create_impl(
        cls,
        *,
        config: Any | None = None,
        config_dict: dict[str, Any] | None = None,
        config_path: str | Path | None = None,
        workspace: str | Path | None = None,
        model: str | None = None,
        provider: Any | None = None,
        profile: Any | None = None,
        bootstrap: Mapping[str, str] | None = None,
        skills: Sequence[Any] | None = None,
        env: Any | None = None,
    ) -> MoekaCore:
        """:meth:`create`'s implementation, unwarned — for internal (in-repo) callers
        (:meth:`scoped`/:meth:`scoped_async`, which warn once themselves) so
        constructing a core through them still emits exactly one warning.

        Build a core from moeka config — files optional.

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
            profile: Agent profile to apply — a name from ``config.profiles``,
                an :class:`~nanobot.config.schema.AgentProfileConfig`, or a plain
                dict. A scoping bundle of model preset, persona, tool allow/deny
                list, skills, memory toggle, and runner limits. Compiled into a
                deep copy of the config so the caller's object is untouched.
            bootstrap: In-memory bootstrap sections (name -> markdown content),
                e.g. ``{"AGENTS.md": persona, "USER.md": profile_text}``. Each
                entry shadows the workspace file of the same name; names outside
                the bootstrap set are appended as extra sections. The profile
                persona flows in here too (under ``"AGENTS.md"``, unless the
                caller supplied that key) — nothing is written to disk.
            skills: In-code skills — :class:`~nanobot.config.schema.InlineSkillConfig`
                instances or equivalent dicts. Combined with the profile's
                ``skills_inline``. Inline skills shadow workspace/builtin skills
                of the same name and bypass ``skills_include`` filtering.
            env: Optional :class:`~nanobot.kernel.env.CoreEnvironment`. When given
                it wins for paths/credentials; ``config`` still drives model and
                provider choice. Default: built from the config by ``LegacyEnvironment``.
        """
        from nanobot.config.loader import config_from_sources

        cfg, from_file = config_from_sources(
            config=config, config_dict=config_dict, config_path=config_path,
        )

        from nanobot.config.profile import (
            apply_profile,
            build_bootstrap_overrides,
            build_inline_skills,
        )

        prof = None
        if profile is not None:
            cfg, prof = apply_profile(cfg, profile)

        overrides = build_bootstrap_overrides(prof, bootstrap)
        inline_skills = build_inline_skills(prof, skills)

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

        core = cls._from_config_impl(
            cfg, workspace=ws, model=model, provider=provider,
            bootstrap_overrides=overrides, inline_skills=inline_skills, env=env,
        )
        core._ephemeral_workspace = ephemeral
        if prof is not None:
            core.profile = prof
            core.profile_name = profile if isinstance(profile, str) else "inline"
        return core

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
        profile: Any | None = None,
        bootstrap: Mapping[str, str] | None = None,
        skills: Sequence[Any] | None = None,
        env: Any | None = None,
    ) -> MoekaCore:
        """Build a core from moeka config — files optional. See :meth:`_create_impl`
        for the full docstring (arguments and return value are unchanged).

        .. deprecated::
            Use ``moeka.Kernel(env)`` plus ``kernel.agent(AgentSpec.from_profile(...))``
            instead — see docs/python-sdk.md.
        """
        warn_deprecated(
            "nanobot.core.MoekaCore.create",
            "moeka.Kernel(env) with kernel.agent(AgentSpec.from_profile(...))",
        )
        return cls._create_impl(
            config=config, config_dict=config_dict, config_path=config_path,
            workspace=workspace, model=model, provider=provider, profile=profile,
            bootstrap=bootstrap, skills=skills, env=env,
        )

    @classmethod
    @contextmanager
    def _scoped_impl(
        cls,
        *,
        profile: Any | None = None,
        workspace: str | Path | None = None,
        **kwargs: Any,
    ) -> Iterator[MoekaCore]:
        """:meth:`scoped`'s implementation, unwarned — see that method, and
        :meth:`_create_impl` on why internal callers use this instead."""
        import shutil
        import tempfile

        owned: Path | None = None
        if workspace is None:
            owned = Path(tempfile.mkdtemp(prefix="moeka-scoped-"))
            workspace = owned
        core = None
        try:
            core = cls._create_impl(profile=profile, workspace=workspace, **kwargs)
            yield core
        finally:
            if core is not None:
                core.cleanup()
            if owned is not None:
                shutil.rmtree(owned, ignore_errors=True)

    @classmethod
    @contextmanager
    def scoped(
        cls,
        *,
        profile: Any | None = None,
        workspace: str | Path | None = None,
        **kwargs: Any,
    ) -> Iterator[MoekaCore]:
        """Context-managed core whose workspace is guaranteed to be cleaned up.

        When *workspace* is omitted an ephemeral temp dir is created and removed
        on exit — even on exceptions — so embedding hosts (pipelines, services)
        can never leak agent workspaces. A supplied *workspace* persists; only
        :meth:`cleanup` is called on exit then. Accepts every :meth:`create`
        keyword (``config_dict``, ``model``, ...)::

            with MoekaCore.scoped(profile="research", config_path=p) as core:
                answer = await core.run("...")   # inside async code, see scoped_async

        .. deprecated::
            Use ``moeka.Kernel(env)`` as a context manager instead — see
            docs/python-sdk.md.
        """
        # stacklevel=4: one deeper than a plain function (see nanobot/_deprecation.py) —
        # @contextmanager's __enter__ calling next(self.gen) adds a frame between the
        # `with` statement and this generator's body.
        warn_deprecated(
            "nanobot.core.MoekaCore.scoped", "moeka.Kernel(env) as a context manager",
            stacklevel=4,
        )
        with cls._scoped_impl(profile=profile, workspace=workspace, **kwargs) as core:
            yield core

    @classmethod
    @asynccontextmanager
    async def scoped_async(
        cls,
        *,
        profile: Any | None = None,
        workspace: str | Path | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[MoekaCore]:
        """Async twin of :meth:`scoped` for hosts already inside an event loop.

        .. deprecated::
            Use ``async with moeka.Kernel(env)`` instead — see docs/python-sdk.md.
        """
        # stacklevel=4: see the matching comment in scoped().
        warn_deprecated(
            "nanobot.core.MoekaCore.scoped_async", "async with moeka.Kernel(env)",
            stacklevel=4,
        )
        with cls._scoped_impl(profile=profile, workspace=workspace, **kwargs) as core:
            yield core

    @classmethod
    def _from_config_impl(
        cls,
        config: Any,
        *,
        workspace: str | Path | None = None,
        model: str | None = None,
        provider: Any | None = None,
        bootstrap_overrides: Mapping[str, str] | None = None,
        inline_skills: Sequence[Any] | None = None,
        env: Any | None = None,
    ) -> MoekaCore:
        """:meth:`from_config`'s implementation, unwarned — see :meth:`_create_impl`
        on why internal callers (:meth:`_create_impl`) use this instead.

        Build a core directly from an in-memory :class:`Config` (the data seam).

        Pure ``(Config, workspace) -> MoekaCore``: it does not read or discover any
        config file. ``workspace`` overrides ``config.agents.defaults.workspace``
        when given; otherwise the config's own workspace is used as-is.
        ``bootstrap_overrides`` and ``inline_skills`` are passed through to the
        loop's context builder (see :meth:`create` for semantics).
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
            "vec_store": cls._build_vec_store(config, env),
        }
        if bootstrap_overrides:
            extra["bootstrap_overrides"] = dict(bootstrap_overrides)
        if inline_skills:
            extra["inline_skills"] = list(inline_skills)
        if provider is not None:
            extra["provider"] = provider
        if model is not None:
            extra["model"] = model
        if env is not None:
            extra["env"] = env

        loop = AgentLoop.from_config(config, tool_registry=ToolRegistry(), **extra)
        return cls(loop)

    @classmethod
    def from_config(
        cls,
        config: Any,
        *,
        workspace: str | Path | None = None,
        model: str | None = None,
        provider: Any | None = None,
        bootstrap_overrides: Mapping[str, str] | None = None,
        inline_skills: Sequence[Any] | None = None,
        env: Any | None = None,
    ) -> MoekaCore:
        """Build a core directly from an in-memory :class:`Config`. See
        :meth:`_from_config_impl` for the full docstring (arguments and return
        value are unchanged).

        .. deprecated::
            Use ``moeka.Kernel(env)`` instead — see docs/python-sdk.md.
        """
        warn_deprecated("nanobot.core.MoekaCore.from_config", "moeka.Kernel(env)")
        return cls._from_config_impl(
            config, workspace=workspace, model=model, provider=provider,
            bootstrap_overrides=bootstrap_overrides, inline_skills=inline_skills, env=env,
        )

    @staticmethod
    def _build_vec_store(config: Any, env: Any | None = None) -> Any | None:
        """Construct the semantic store when enabled (degrades gracefully).

        The product runtime never instantiates a VecStore, so the core wires it
        up here — at ``<workspace>/memory/vec.db`` — to make RAG over memory,
        history, and host documents actually work. Returns ``None`` when disabled;
        an unavailable store (``moeka[vec]`` missing) is harmless and inert.
        With an explicit host *env* the workspace is ``env.paths.work_dir`` (the
        loop's workspace; memory files stay in the work dir, R4).
        """
        vec_config = config.agents.defaults.vec
        if not getattr(vec_config, "enable", False):
            return None
        from nanobot.core.vec_store import VecStore

        workspace = env.paths.work_dir if env is not None else config.workspace_path
        db_path = workspace / "memory" / "vec.db"
        return VecStore(
            db_path,
            model_name=vec_config.embedding_model,
            log_retrievals=getattr(vec_config, "log_retrievals", False),
        )

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
        """Close the stores, then remove the ephemeral workspace if we made one.

        The store teardown runs **unconditionally**, including when the host
        supplied its own workspace. Previously this only rmtree'd the ephemeral
        directory, so every core leaked an open SQLite connection for
        ``sessions.db`` and another for ``vec.db`` (plus the loaded
        SentenceTransformer). A host creating many scoped cores accumulated one
        pair each, and the unclosed vec.db connection is why an orphaned 13 MB
        WAL survived on jifan against a 1.7 MB database — nothing ever
        checkpointed it.

        Order matters: close before rmtree, or the ephemeral case unlinks files
        out from under live connections.
        """
        self._close_epistemic_stores()
        for store_name in ("vec_store", "sessions"):
            store = getattr(self._loop, store_name, None)
            closer = getattr(store, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # never let teardown mask the caller's error
                    logger.exception("MoekaCore.cleanup: failed to close %s", store_name)

        ws = self._ephemeral_workspace
        if ws is None:
            return
        import shutil

        shutil.rmtree(ws, ignore_errors=True)
        self._ephemeral_workspace = None

    # ------------------------------------------------------------------
    # Epistemic stores (P5, design I3 / section 7)
    # ------------------------------------------------------------------

    @property
    def env(self) -> Any | None:
        """The :class:`~nanobot.kernel.env.CoreEnvironment` the loop runs with.

        The explicit ``env=`` given to :meth:`create`/:meth:`from_config`, else the
        loop's ``LegacyEnvironment`` (flat layout: ``state_dir`` is the workspace).
        ``None`` only for a hand-built loop that carries no ``CoreEnvironment``.
        """
        from nanobot.kernel.env import CoreEnvironment

        env = getattr(self._loop, "env", None)
        return env if isinstance(env, CoreEnvironment) else None

    @property
    def facts(self) -> Any | None:
        """The kernel's :class:`~nanobot.kernel.facts.FactStore` (values with provenance).

        Built on first access with ``FactStore.from_env(self.env)``
        (``<state_dir>/facts.db``, tracing to ``env.trace``) unless the host assigned
        one. ``None`` when there is no :attr:`env` (graceful degradation; nothing is
        created on disk until first access).
        """
        if self._facts is None:
            env = self.env
            if env is None:
                return None
            from nanobot.kernel.facts import FactStore

            self._facts = FactStore.from_env(env)
            self._owns_facts = True
        return self._facts

    @facts.setter
    def facts(self, store: Any) -> None:
        if self._artifacts is not None and self._artifacts.facts is not store:
            if not self._owns_artifacts:
                raise ValueError(
                    "the assigned artifact store resolves cites against another fact "
                    "store; assign a matching artifact store"
                )
            self._close_owned("artifacts")  # rebuilt over the new fact store on next use
        self._close_owned("facts")
        self._facts = store
        self._owns_facts = False

    @property
    def artifacts(self) -> Any | None:
        """The kernel's :class:`~nanobot.kernel.artifacts.ArtifactStore` (I3 typed artifacts).

        Built on first access with ``ArtifactStore.from_env(self.env, facts=self.facts)``
        (``<state_dir>/artifacts.db``), so it resolves cites against :attr:`facts`,
        unless the host assigned one. ``None`` when there is no :attr:`env`. Kinds are
        registered on it by the host: ``kernel.artifacts.register_kind(name, model)``.
        """
        if self._artifacts is None:
            env = self.env
            facts = self.facts
            if env is None or facts is None:
                return None
            from nanobot.kernel.artifacts import ArtifactStore

            self._artifacts = ArtifactStore.from_env(env, facts=facts)
            self._owns_artifacts = True
        return self._artifacts

    @artifacts.setter
    def artifacts(self, store: Any) -> None:
        if self._facts is not None and store.facts is not self._facts and not self._owns_facts:
            raise ValueError(
                "the artifact store resolves cites against another fact store than the "
                "assigned kernel.facts; assign its fact store to kernel.facts first"
            )
        self._close_owned("artifacts")
        if self._facts is not store.facts:
            self._close_owned("facts")
            self._facts = store.facts
            self._owns_facts = False
        self._artifacts = store
        self._owns_artifacts = False

    def _require_artifacts(self) -> Any:
        artifacts = self.artifacts
        if artifacts is None:
            raise RuntimeError(
                "no CoreEnvironment: this kernel has no fact/artifact store; build it "
                "with MoekaKernel.create(env=...) or assign kernel.facts/kernel.artifacts"
            )
        return artifacts

    def propose(
        self,
        kind: str,
        delta: dict[str, Any],
        cites: dict[str, str] | None = None,
        *,
        artifact_id: str | None = None,
    ) -> Any:
        """Merge ``delta`` into an artifact: cited leaves commit, uncited stay provisional.

        Thin passthrough to :meth:`ArtifactStore.propose
        <nanobot.kernel.artifacts.ArtifactStore.propose>` on :attr:`artifacts`
        (``cites`` maps a dotted leaf path to a :attr:`facts` trace ID). Raises
        ``RuntimeError`` when there is no store (no :attr:`env`).
        """
        return self._require_artifacts().propose(kind, delta, cites, artifact_id=artifact_id)

    def answer(self, question: Any, answer: Any, turn_ref: str) -> Any:
        """Commit the user's ``answer`` to a clarification ``question``'s leaf.

        Thin passthrough to :func:`nanobot.kernel.clarify.record_answer`: records the
        answer as a ``user`` fact (``source_ref=turn_ref``) in :attr:`facts`, then
        commits it through :attr:`artifacts` citing that fact. Raises ``RuntimeError``
        when there is no store (no :attr:`env`).
        """
        artifacts = self._require_artifacts()
        from nanobot.kernel.clarify import record_answer

        return record_answer(artifacts.facts, artifacts, question, answer, turn_ref)

    def _close_owned(self, which: str) -> None:
        owned = self._owns_artifacts if which == "artifacts" else self._owns_facts
        store = self._artifacts if which == "artifacts" else self._facts
        if store is None or not owned:
            return
        try:
            store.close()
        except Exception:  # never let teardown mask the caller's error
            logger.exception("MoekaCore: failed to close the {} store", which)
        if which == "artifacts":
            self._artifacts, self._owns_artifacts = None, False
        else:
            self._facts, self._owns_facts = None, False

    def _close_epistemic_stores(self) -> None:
        """Close the stores this kernel built (artifacts first); injected ones stay open."""
        self._close_owned("artifacts")
        self._close_owned("facts")

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
    # In-memory context: bootstrap sections and skills
    # ------------------------------------------------------------------

    def set_bootstrap(self, name: str, content: str) -> None:
        """Set an in-memory bootstrap section (e.g. ``"AGENTS.md"``, ``"USER.md"``).

        Shadows the workspace file of the same name; unknown names become extra
        sections. The system prompt is rebuilt every turn, so this takes effect
        on the next :meth:`run`.
        """
        self._loop.context.bootstrap_overrides[name] = content

    def add_skill(
        self,
        name: str,
        content: str,
        *,
        description: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Register an in-code skill on the live core (effective next turn).

        ``metadata`` mirrors SKILL.md frontmatter (``always``, ``requires``).
        Shadows workspace/builtin skills of the same name; exempt from
        ``skills_include`` filtering.
        """
        from nanobot.config.schema import InlineSkillConfig

        skill = InlineSkillConfig(
            name=name, content=content, description=description, metadata=metadata or {},
        )
        self._loop.context.skills.inline_skills[name] = skill

    # ------------------------------------------------------------------
    # Documents — RAG over host-supplied knowledge
    # ------------------------------------------------------------------

    @property
    def vec_available(self) -> bool:
        """True when semantic retrieval is usable (``moeka[vec]`` installed)."""
        vs = self._loop.vec_store
        return bool(vs is not None and vs.available)

    def ingest(
        self,
        text_or_path: str | Path,
        *,
        source: str | None = None,
        collection: str = "default",
        tags: list[str] | None = None,
    ) -> int:
        """Ingest text or a document file into the host-document collection.

        ``text_or_path`` is treated as a file path if it points at an existing
        file, otherwise as raw text. ``tags`` are stored with every chunk and
        filterable at retrieval time. Returns the number of chunks indexed
        (0 when ``moeka[vec]`` is not installed). For text that must never be
        path-detected, use :meth:`ingest_text`.
        """
        vs = self._loop.vec_store
        if vs is None or not vs.available:
            return 0

        text, src = self._resolve_ingest_input(text_or_path, source)
        if not text.strip():
            return 0
        return vs.add_documents(text, source=src, collection=collection, tags=tags)

    def ingest_text(
        self,
        text: str,
        *,
        source: str | None = None,
        collection: str = "default",
        tags: list[str] | None = None,
    ) -> int:
        """Ingest raw *text* verbatim — never path-detects.

        The explicit contract :meth:`ingest` can't give: arbitrary host/corpus
        text is indexed as-is even if it happens to name an existing file.
        Returns the number of chunks indexed (0 when ``moeka[vec]`` is missing).
        """
        vs = self._loop.vec_store
        if vs is None or not vs.available:
            return 0
        if not text.strip():
            return 0
        return vs.add_documents(text, source=source, collection=collection, tags=tags)

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

    def retrieve(
        self,
        query: str,
        *,
        k: int = 5,
        collection: str | None = "default",
        mode: str = "vec",
        tags: list[str] | None = None,
        since: str | None = None,
        caller: str | None = None,
    ) -> list[str]:
        """Return the top-k host-document chunks closest to *query*.

        ``mode`` may be ``"vec"``, ``"keyword"`` (FTS5; no embeddings needed),
        or ``"hybrid"``; ``tags``/``since`` filter by stored metadata; ``caller``
        labels the retrieval-log entry when logging is enabled.
        """
        vs = self._loop.vec_store
        if vs is None or not vs.available:
            return []
        return vs.search_documents(
            query, k=k, collection=collection, mode=mode,
            tags=tags, since=since, caller=caller,
        )

    def retrieve_documents(
        self,
        query: str,
        *,
        k: int = 5,
        collection: str | None = "default",
        mode: str = "vec",
        tags: list[str] | None = None,
        since: str | None = None,
        caller: str | None = None,
    ) -> list[RetrievedChunk]:
        """Structured retrieval: top-k chunks with source attribution and score.

        Unlike :meth:`retrieve` (bare strings), each result carries *which*
        document it came from and *how close* it was — for thresholding,
        dedupe, and doc-level assembly. ``collection=None`` searches all
        collections. Empty list when ``moeka[vec]`` is unavailable.
        """
        vs = self._loop.vec_store
        if vs is None or not vs.available:
            return []
        return [
            RetrievedChunk(text=text, source=source, score=score)
            for source, text, score in vs.search_documents_scored(
                query, k=k, collection=collection, mode=mode,
                tags=tags, since=since, caller=caller,
            )
        ]

    def count_documents(self, *, collection: str | None = "default") -> int:
        """Number of indexed host-document chunks (0 when vec is unavailable)."""
        vs = self._loop.vec_store
        if vs is None or not vs.available:
            return 0
        return vs.count_documents(collection=collection)

    def clear_documents(self, *, collection: str | None = "default") -> None:
        """Delete indexed host documents for deterministic reindexing.

        ``collection=None`` clears every collection. No-op when vec is
        unavailable.
        """
        vs = self._loop.vec_store
        if vs is not None and vs.available:
            vs.clear_documents(collection=collection)

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
        on_token: Callable[[str], Awaitable[None]] | None = None,
    ) -> RunResult:
        """Run one agent turn (multi-step tool calling + RAG context) and return it.

        Different ``session_key`` values get independent conversation history.
        ``on_token`` is an async callback awaited with each streamed text delta
        as the model produces it; the full result is still returned at the end.
        """
        capture = SDKCaptureHook()
        prev = self._loop._extra_hooks
        base_hooks = list(hooks) if hooks is not None else list(prev or [])
        self._loop._extra_hooks = [capture, *base_hooks]
        try:
            response = await self._loop.process_direct(
                message, session_key=session_key, media=media, on_stream=on_token,
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

    @staticmethod
    def complete_sync(prompt: str, **kwargs: Any) -> str:
        """Synchronous one-shot completion (no agent loop).

        Thin delegate to :func:`nanobot.api.complete.complete`; raises when
        called inside a running event loop — await :meth:`complete` there.
        """
        from nanobot.api.complete import complete

        return complete(prompt, **kwargs)

    @staticmethod
    async def think_structured(
        prompt: str,
        *,
        schema: dict[str, Any] | None = None,
        model_cls: type | None = None,
        retries: int = 2,
        task_type: str | None = None,
        slot: str | None = None,
        verify: Callable[[Any], Any] | None = None,
        tier: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """One-shot structured thinking: a completion constrained to JSON.

        Delegates to :func:`nanobot.api.complete.acomplete_json` — a
        provider-agnostic parse-retry loop (no native JSON mode required).
        Pass ``schema`` (JSON Schema dict) or ``model_cls`` (pydantic model;
        validated instance is returned). Per-call ``model`` / ``temperature``
        / ``max_tokens`` / ``system`` / ``images`` forward to the provider.
        ``task_type`` (and ``task_payload``) select a registered deterministic
        solver first; a solved task makes no LLM call (I6).

        Routing (I6, :func:`nanobot.kernel.router.route`): with ``slot``,
        ``verify`` or ``tier`` the call goes through the cost-aware router
        (solver -> fast tier -> ``verify`` -> one tier up on a failure; tier
        ceilings from ``config.router.slots``). ``policy`` / ``principal`` /
        ``env`` / ``max_escalations`` then pass to the router. Raises
        :class:`~nanobot.kernel.router.ModelDispatchDeniedError` when the router's
        ``model.dispatch`` request is denied and
        :class:`~nanobot.kernel.router.RouteVerificationError` when no tier
        passes ``verify``. Without them the behaviour is unchanged.
        """
        complete_api = importlib.import_module("nanobot.api.complete")

        if slot is None and verify is None and tier is None:
            if task_type is not None:
                kwargs["task_type"] = task_type
            return await complete_api.acomplete_json(
                prompt, schema=schema, model_cls=model_cls, retries=retries, **kwargs
            )

        from nanobot.kernel import router

        payload = kwargs.pop("task_payload", None)
        result = await router.route(
            slot or "default",
            task_type,
            payload,
            prompt=prompt,
            verify=verify,
            tier=tier,
            model_cls=model_cls,
            schema=schema,
            retries=retries,
            **kwargs,
        )
        if result.denied:
            raise router.ModelDispatchDeniedError(result)
        if result.verified is False:
            raise router.RouteVerificationError(result)
        return result.value


MoekaKernel = MoekaCore
