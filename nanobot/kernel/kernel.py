"""``Kernel``: the host-facing engine object built from an :class:`Environment`.

It owns the environment, the :class:`~nanobot.kernel.trace.Tracer`, a private loop thread (the sync-twin
bridge) and the LLM layer (``kernel.llm``), and has an idempotent close in both
sync and async form. Agents, sessions and memory attach here.

The host may pass a :class:`~nanobot.kernel.budget.Budget` (every model call is
admitted against it, see :mod:`nanobot.kernel.budget`), a
:class:`~nanobot.kernel.budget.ResponseCache`, and ``max_concurrency`` (the
default concurrency of ``kernel.llm.batch``).

Per kernel (Task 8): ``variant`` (a :class:`~nanobot.kernel.variants.Variant`, stored as
``kernel.variant`` and handed to the agents the kernel builds) and the ``solvers`` /
``baselines`` registries. ``None`` registries are the process-wide defaults (unchanged
behaviour); a kernel given its own registry never sees solvers registered elsewhere.
``kernel.llm.complete_json``'s deterministic fast path consults ``kernel.solvers``.

Sessions (Task 10): ``kernel.sessions`` hands out :class:`~nanobot.kernel.sessions.Session`
handles over that same store (append, checkpoint, fork, rewind, snapshot/restore),
sharing the per-key locks agent runs take.

Agents (Task 9): ``kernel.agent(spec)`` returns the :class:`~nanobot.kernel.agent.Agent`
for an ``AgentSpec`` (cached per equal spec). All agents share one ``SessionManager``
under ``env.paths.state_dir``; ``policy`` (optional) is intersected with each spec's.
``plugins`` (a :class:`~nanobot.kernel.registry.PluginRegistry`) switches every agent's
tool loading, and its sub-agents', to kernel mode: an entry-point plugin loads only
when the registry has it active. ``None`` keeps legacy entry-point loading.
``close``/``aclose`` closes the agents, then the session store, then the LLM
providers, then the loop thread.

Trace routing: ``kernel.trace`` is a :class:`~nanobot.kernel.trace.Tracer` wrapping
``env.trace`` (the host's sink). Everything the kernel builds (pool providers, their
ledgers, budget metering, cache events) is handed ``kernel.core_env``, a copy of
``env.core`` whose ``trace`` is that Tracer, so subscribers see every kernel-internal
event. The host's ``Environment`` itself is left untouched.
"""

from __future__ import annotations

import asyncio
import dataclasses
import threading
from types import TracebackType
from typing import TYPE_CHECKING

from loguru import logger

from nanobot.kernel.bridge import LoopThread
from nanobot.kernel.hostenv import Environment
from nanobot.kernel.trace import Tracer

if TYPE_CHECKING:
    from nanobot.kernel.agent import Agent, AgentSpec
    from nanobot.kernel.baselines import BaselineRegistry
    from nanobot.kernel.budget import Budget, ResponseCache
    from nanobot.kernel.env import CoreEnvironment
    from nanobot.kernel.llm import LLM
    from nanobot.kernel.policy import PermissionPolicy
    from nanobot.kernel.registry import PluginRegistry
    from nanobot.kernel.sessions import Sessions
    from nanobot.kernel.solvers import SolverRegistry
    from nanobot.kernel.variants import Variant
    from nanobot.session.manager import SessionManager


class Kernel:
    """One kernel per host environment. Use as a (sync or async) context manager,
    or call :meth:`close` / :meth:`aclose` when done; both are idempotent."""

    def __init__(
        self,
        env: Environment,
        *,
        budget: Budget | None = None,
        cache: ResponseCache | None = None,
        variant: Variant | None = None,
        policy: PermissionPolicy | None = None,
        plugins: PluginRegistry | None = None,
        max_concurrency: int = 16,
        solvers: SolverRegistry | None = None,
        baselines: BaselineRegistry | None = None,
    ) -> None:
        if not isinstance(env, Environment):
            raise TypeError(
                f"Kernel(env) needs a moeka Environment, got {type(env).__name__}; "
                "build one with Environment.for_host(...) or Environment.from_config(...)"
            )
        from nanobot.kernel import baselines as _baselines
        from nanobot.kernel import solvers as _solvers
        from nanobot.kernel.budget import Budget, ResponseCache
        from nanobot.kernel.variants import Variant

        if budget is not None and not isinstance(budget, Budget):
            raise TypeError(
                f"budget must implement admit/settle/release, got {type(budget).__name__}"
            )
        if cache is not None and not isinstance(cache, ResponseCache):
            raise TypeError(f"cache must implement get/put, got {type(cache).__name__}")
        if variant is not None and not isinstance(variant, Variant):
            raise TypeError(f"variant must be a Variant, got {type(variant).__name__}")
        if policy is not None and not callable(getattr(policy, "decide", None)):
            raise TypeError(
                f"policy must implement decide(principal, request, ctx), got {type(policy).__name__}"
            )
        if plugins is not None:
            from nanobot.kernel.registry import PluginRegistry

            if not isinstance(plugins, PluginRegistry):
                raise TypeError(f"plugins must be a PluginRegistry, got {type(plugins).__name__}")
        if solvers is not None and not isinstance(solvers, _solvers.SolverRegistry):
            raise TypeError(f"solvers must be a SolverRegistry, got {type(solvers).__name__}")
        if baselines is not None and not isinstance(baselines, _baselines.BaselineRegistry):
            raise TypeError(
                f"baselines must be a BaselineRegistry, got {type(baselines).__name__}"
            )
        if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) \
                or max_concurrency < 1:
            raise ValueError(f"max_concurrency must be a positive int, got {max_concurrency!r}")
        self._env = env
        self._tracer = Tracer(env.trace)
        self._core_env = dataclasses.replace(env.core, trace=self._tracer)
        self._budget = budget
        self._cache = cache
        self._max_concurrency = max_concurrency
        self._variant = variant
        self._policy = policy
        self._plugins = plugins
        # ``None`` = the process-wide default, looked up on each access.
        self._solvers = solvers
        self._baselines = baselines
        self._closed = False
        self._closing = False  # set once close() starts: no new agents or session stores
        self._close_lock = threading.Lock()
        # Internal: the loop that ``*_sync`` twins run on. Starts lazily on first use.
        self._bridge = LoopThread(name="moeka-kernel-loop")
        self._llm: LLM | None = None
        # Agents (Task 9): one per spec, sharing one SessionManager (both built lazily).
        self._agents: dict[AgentSpec, Agent] = {}
        self._sessions: SessionManager | None = None
        self._session_handles: Sessions | None = None
        # Not _close_lock: a run building its loop on the loop thread must never wait
        # on a close() that is itself waiting for the loop thread.
        self._agents_lock = threading.Lock()
        # Per session key, across all agents (runs execute on the kernel loop thread).
        self._session_locks: dict[str, asyncio.Lock] = {}

    @property
    def env(self) -> Environment:
        return self._env

    @property
    def trace(self) -> Tracer:
        """The kernel's :class:`Tracer` (forwards to ``env.trace``; ``span``/``subscribe``)."""
        return self._tracer

    @property
    def core_env(self) -> CoreEnvironment:
        """``env.core`` with ``trace`` set to :attr:`trace`: what kernel components get."""
        return self._core_env

    @property
    def budget(self) -> Budget | None:
        return self._budget

    @property
    def cache(self) -> ResponseCache | None:
        return self._cache

    @property
    def max_concurrency(self) -> int:
        return self._max_concurrency

    @property
    def variant(self) -> Variant | None:
        """The kernel's :class:`Variant` (``None`` = built-ins); agents it builds use it."""
        return self._variant

    @property
    def policy(self) -> PermissionPolicy | None:
        """The kernel's permission policy; each agent's is this ∩ ``AgentSpec.policy``."""
        return self._policy

    @property
    def plugins(self) -> PluginRegistry | None:
        """The kernel's plugin registry (``None``: agents load plugins the legacy way)."""
        return self._plugins

    @property
    def solvers(self) -> SolverRegistry:
        """This kernel's solver registry (the process-wide default unless one was passed)."""
        if self._solvers is not None:
            return self._solvers
        from nanobot.kernel.solvers import default_registry

        return default_registry()

    @property
    def baselines(self) -> BaselineRegistry:
        """This kernel's baseline registry (the process-wide default unless one was passed)."""
        if self._baselines is not None:
            return self._baselines
        from nanobot.kernel.baselines import default_registry

        return default_registry()

    @property
    def llm(self) -> LLM:
        """The model-call layer (created on first access, one per kernel)."""
        llm = self._llm
        if llm is None:
            with self._close_lock:
                if self._closed:
                    raise RuntimeError("kernel is closed")
                if self._llm is None:
                    from nanobot.kernel.llm import LLM

                    self._llm = LLM(self)
                llm = self._llm
        return llm

    @property
    def sessions(self) -> Sessions:
        """Session handles over the shared store (created on first access; see
        :mod:`nanobot.kernel.sessions`)."""
        handles = self._session_handles
        if handles is None:
            with self._agents_lock:
                if self._closed or self._closing:
                    raise RuntimeError("kernel is closed")
                if self._session_handles is None:
                    from nanobot.kernel.sessions import Sessions

                    self._session_handles = Sessions(self)
                handles = self._session_handles
        return handles

    @property
    def closed(self) -> bool:
        return self._closed

    # -- agents ------------------------------------------------------------

    def agent(self, spec: AgentSpec) -> Agent:
        """The agent for *spec* (one per equal spec; its loop is built on first use)."""
        from nanobot.kernel.agent import Agent, AgentSpec

        if not isinstance(spec, AgentSpec):
            raise TypeError(f"agent() needs an AgentSpec, got {type(spec).__name__}")
        with self._agents_lock:
            if self._closed or self._closing:
                raise RuntimeError("kernel is closed")
            agent = self._agents.get(spec)
            if agent is None or agent.closed:
                agent = Agent(self, spec)
                self._agents[spec] = agent
            return agent

    def _session_lock(self, key: str) -> asyncio.Lock:
        """The kernel-wide lock for one session key (use on the kernel loop only)."""
        lock = self._session_locks.get(key)
        if lock is None:
            lock = self._session_locks[key] = asyncio.Lock()
        return lock

    def _session_manager(self) -> SessionManager:
        """The one ``SessionManager`` all of this kernel's agents share (state dir)."""
        with self._agents_lock:
            if self._closed or self._closing:
                raise RuntimeError("kernel is closed")
            if self._sessions is None:
                from nanobot.session.manager import SessionManager
                from nanobot.session.sqlite_store import SqliteSessionStore

                paths = self._env.paths
                store = SqliteSessionStore(paths.work_dir, sessions_root=paths.sessions_root)
                self._sessions = SessionManager(
                    paths.work_dir, sessions_root=paths.sessions_root, store=store,
                )
            return self._sessions

    # -- lifecycle -------------------------------------------------------

    def _reject_loop_thread(self, method: str) -> None:
        if threading.current_thread() is self._bridge._thread:
            raise RuntimeError(
                f"Kernel.{method}() called from the kernel's own loop thread would join "
                "itself; close the kernel from host code instead"
            )

    def close(self) -> None:
        """Release kernel resources and stop the loop thread. Idempotent.

        Raises ``RuntimeError`` when called from the kernel's own loop thread
        (it would join itself), leaving the kernel open. Concurrent callers
        block until the first close has finished, so a return always means
        the loop thread is gone.
        """
        self._reject_loop_thread("close")
        with self._close_lock:
            if self._closed:
                return
            with self._agents_lock:
                self._closing = True
            self._close_agents()
            self._close_llm()
            self._bridge.stop()
            # Only after stop() returned: a failed stop leaves the kernel open
            # so a later close() can retry.
            self._closed = True

    def _close_agents(self) -> None:
        # Agents' loops (and their in-flight runs) live on the loop thread; then the
        # shared session store, once no agent can write to it.
        with self._agents_lock:
            agents = list(self._agents.values())
            self._agents.clear()
        if agents and self._bridge._state == "running":
            try:
                self._bridge.run(self._aclose_agents(agents), timeout=30.0)
            except Exception as exc:  # noqa: BLE001 - closing must not fail the kernel close
                logger.warning("kernel: closing agents failed: {!r}", exc)
        with self._agents_lock:
            sessions, self._sessions = self._sessions, None
        if sessions is not None:
            try:
                sessions.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("kernel: closing the session store failed: {!r}", exc)

    @staticmethod
    async def _aclose_agents(agents: list[Agent]) -> None:
        for agent in agents:
            try:
                await agent._aclose()
            except BaseException as exc:  # noqa: BLE001 - close the others regardless
                logger.warning("kernel: closing agent {!r} failed: {!r}", agent.spec.name, exc)

    def _close_llm(self) -> None:
        # Pool providers' HTTP clients live on the loop thread: close them there,
        # before it stops. Never started = no client was ever used.
        if self._llm is None or self._bridge._state != "running":
            return
        try:
            self._bridge.run(self._llm._aclose(), timeout=10.0)
        except Exception as exc:  # noqa: BLE001 - closing must not fail the kernel close
            logger.warning("kernel: closing LLM providers failed: {!r}", exc)

    async def aclose(self) -> None:
        """Async :meth:`close`: stops the loop thread off the caller's event loop."""
        self._reject_loop_thread("aclose")
        # ``_closed`` is set only once the thread is stopped, so this fast path
        # never returns while another thread's close is still in progress.
        if self._closed:
            return
        await asyncio.to_thread(self.close)

    def __enter__(self) -> Kernel:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    async def __aenter__(self) -> Kernel:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def __repr__(self) -> str:
        state = "closed" if self._closed else "open"
        return f"Kernel({self._env!r}, {state})"


__all__ = ["Kernel"]
