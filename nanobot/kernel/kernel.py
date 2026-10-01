"""``Kernel``: the host-facing engine object built from an :class:`Environment`.

It owns the environment, the :class:`~nanobot.kernel.trace.Tracer`, a private loop thread (the sync-twin
bridge) and the LLM layer (``kernel.llm``), and has an idempotent close in both
sync and async form. Agents, sessions and memory attach here.

The host may pass a :class:`~nanobot.kernel.budget.Budget` (every model call is
admitted against it, see :mod:`nanobot.kernel.budget`), a
:class:`~nanobot.kernel.budget.ResponseCache`, and ``max_concurrency`` (the
default concurrency of ``kernel.llm.batch``), and ``action_workers`` (the threads of
the pool agents' sync host actions run on).

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

Memory and epistemics (Task 11): ``kernel.memory(scope)`` / ``kernel.memory(path=...)``
returns a :class:`~nanobot.kernel.memory.DocStore` (``<state_dir>/memory/<scope>.db``;
all of a kernel's stores share one embedder, and at most
:data:`~nanobot.kernel.memory.DEFAULT_MAX_OPEN` files stay open, least recently used
released first). ``kernel.epistemics`` is the
:class:`~nanobot.kernel.epistemics.Epistemics` facade (facts, cited artifacts,
clarification), built from ``kernel.core_env`` so its events reach ``kernel.trace``.
Both are closed by ``close``/``aclose`` after the agents.

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
import weakref
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
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
    from nanobot.kernel.epistemics import Epistemics
    from nanobot.kernel.llm import LLM
    from nanobot.kernel.memory import DocStore, _MemoryRegistry
    from nanobot.kernel.policy import PermissionPolicy
    from nanobot.kernel.registry import PluginRegistry
    from nanobot.kernel.sessions import Sessions
    from nanobot.kernel.solvers import SolverRegistry
    from nanobot.kernel.usage import UsageView
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
        action_workers: int = 8,
        consumer: str | None = None,
        attach: str = "write",
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
        if isinstance(action_workers, bool) or not isinstance(action_workers, int) \
                or action_workers < 1:
            raise ValueError(f"action_workers must be a positive int, got {action_workers!r}")
        if consumer is not None and (not isinstance(consumer, str) or not consumer.strip()):
            raise ValueError(f"consumer must be a non-empty string, got {consumer!r}")
        if attach not in ("write", "read_only"):
            raise ValueError(f"attach must be 'write' or 'read_only', got {attach!r}")
        # Spec 005: one writer per state dir. The lock comes first, before anything
        # that creates a file; a refused writer leaves no trace. A read-only attach
        # takes no lock and creates nothing under the state dir.
        from nanobot.kernel.instance_lock import ReadOnlyKernelError, acquire_instance_lock

        self._read_only = attach == "read_only"
        self._instance_lock = None
        self._lock_finalizer: weakref.finalize | None = None
        if self._read_only:
            if not env.paths.state_dir.is_dir():
                raise ReadOnlyKernelError(
                    f"cannot attach read-only: state dir {env.paths.state_dir} does not exist"
                )
        else:
            lock = acquire_instance_lock(env.paths.state_dir, mode="write")
            self._instance_lock = lock
            # A kernel dropped without close() still frees its state dir.
            self._lock_finalizer = weakref.finalize(self, lock.release)
        try:
            self._init_rest(
                env, budget=budget, cache=cache, variant=variant, policy=policy,
                plugins=plugins, max_concurrency=max_concurrency, solvers=solvers,
                baselines=baselines, action_workers=action_workers, consumer=consumer,
            )
        except BaseException:
            self._release_instance_lock()
            raise

    def _init_rest(
        self,
        env: Environment,
        *,
        budget: Budget | None,
        cache: ResponseCache | None,
        variant: Variant | None,
        policy: PermissionPolicy | None,
        plugins: PluginRegistry | None,
        max_concurrency: int,
        solvers: SolverRegistry | None,
        baselines: BaselineRegistry | None,
        action_workers: int,
        consumer: str | None,
    ) -> None:
        self._consumer = consumer
        self._env = env
        self._tracer = Tracer(env.trace)
        # The cost ledger's usage store, shared with other kernels on this state_dir:
        # held for the kernel's life, released (closed by the last holder) on close.
        from nanobot.llm_usage import acquire_llm_usage_store

        acquire_llm_usage_store(data_dir=env.core.paths.data_dir)
        self._holds_usage_store = True
        self._core_env = dataclasses.replace(
            env.core, trace=self._tracer,
            consumer=consumer if consumer is not None else env.core.consumer,
        )
        self._budget = budget
        attach_trace = getattr(budget, "_attach_trace", None)
        if callable(attach_trace):
            attach_trace(self._tracer)
        self._cache = cache
        self._max_concurrency = max_concurrency
        # Sync host actions' worker threads (built on first use, shut down on close).
        self._action_workers = action_workers
        self._action_pool: ThreadPoolExecutor | None = None
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
        # Agents (Task 9): one per spec, sharing one SessionManager (both built lazily).
        self._agents: dict[AgentSpec, Agent] = {}
        self._sessions: SessionManager | None = None
        self._session_handles: Sessions | None = None
        # Not _close_lock: a run building its loop on the loop thread must never wait
        # on a close() that is itself waiting for the loop thread.
        self._agents_lock = threading.Lock()
        # Per session key, across all agents (runs execute on the kernel loop thread).
        self._session_locks: dict[str, asyncio.Lock] = {}
        # Task 11: document stores and the epistemic facade (both built lazily).
        self._memory_registry: _MemoryRegistry | None = None
        self._epistemics: Epistemics | None = None
        # Built eagerly (cheap: no provider is made until a call needs one). A lazy
        # build would need a lock that close() holds while it waits on the loop
        # thread, where an agent's first build asks for ``kernel.llm``.
        from nanobot.kernel.llm import LLM

        self._llm: LLM = LLM(self)
        from nanobot.kernel.usage import UsageView

        self._usage = UsageView(self)

    @property
    def env(self) -> Environment:
        return self._env

    @property
    def read_only(self) -> bool:
        """True for ``Kernel(env, attach="read_only")`` (no lock; mutating calls refused)."""
        return self._read_only

    def _refuse_write(self, what: str) -> None:
        """Raise ``ReadOnlyKernelError`` for *what* on a read-only kernel."""
        if self._read_only:
            from nanobot.kernel.instance_lock import ReadOnlyKernelError

            raise ReadOnlyKernelError(
                f"{what} is not allowed on a read-only Kernel "
                f"(state dir {self._env.paths.state_dir})"
            )

    def _release_instance_lock(self) -> None:
        lock, self._instance_lock = self._instance_lock, None
        finalizer, self._lock_finalizer = self._lock_finalizer, None
        if finalizer is not None:
            finalizer.detach()
        if lock is not None:
            lock.release()

    @property
    def consumer(self) -> str | None:
        """The default ``consumer`` of every call this kernel makes (``None`` = unattributed)."""
        return self._consumer

    @property
    def usage(self) -> UsageView:
        """Usage, spend and budget for this kernel's calls (see :mod:`nanobot.kernel.usage`)."""
        return self._usage

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
        """The model-call layer (one per kernel). ``RuntimeError`` once close started."""
        if self._closed or self._closing:
            raise RuntimeError("kernel is closed")
        return self._llm

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
    def epistemics(self) -> Epistemics:
        """Facts with provenance, cited artifacts and clarification (created on first
        access; see :mod:`nanobot.kernel.epistemics`)."""
        epi = self._epistemics
        if epi is None:
            with self._agents_lock:
                if self._closed or self._closing:
                    raise RuntimeError("kernel is closed")
                if self._epistemics is None:
                    from nanobot.kernel.epistemics import Epistemics

                    if self._read_only:
                        from nanobot.kernel.epistemics import ReadOnlyEpistemics

                        self._epistemics = ReadOnlyEpistemics(self._core_env)
                    else:
                        self._epistemics = Epistemics(self._core_env)
                epi = self._epistemics
        return epi

    def memory(self, scope: str | None = None, *, path: str | Path | None = None) -> DocStore:
        """The document store for *scope* (``<state_dir>/memory/<scope>.db``; ``None`` is
        ``"default"``) or for a host-chosen file (``path=``). One handle per file; see
        :mod:`nanobot.kernel.memory` for scope names, the shared embedder and the LRU."""
        return self._memory_stores().get(scope, path)

    def _memory_stores(self) -> _MemoryRegistry:
        registry = self._memory_registry
        if registry is None:
            with self._agents_lock:
                if self._closed or self._closing:
                    raise RuntimeError("kernel is closed")
                if self._memory_registry is None:
                    from nanobot.kernel.memory import _MemoryRegistry

                    vec = self._env.config.agents.defaults.vec
                    self._memory_registry = _MemoryRegistry(
                        self._env.paths.state_dir,
                        embedding_model=vec.embedding_model,
                        log_retrievals=vec.log_retrievals and not self._read_only,
                        read_only=self._read_only,
                    )
                registry = self._memory_registry
        return registry

    @property
    def closed(self) -> bool:
        return self._closed

    # -- agents ------------------------------------------------------------

    def agent(self, spec: AgentSpec) -> Agent:
        """The agent for *spec* (one per equal spec; its loop is built on first use)."""
        from nanobot.kernel.agent import Agent, AgentSpec

        self._refuse_write("kernel.agent()")
        if not isinstance(spec, AgentSpec):
            raise TypeError(f"agent() needs an AgentSpec, got {type(spec).__name__}")
        shared: tuple[str, str, str] | None = None
        with self._agents_lock:
            if self._closed or self._closing:
                raise RuntimeError("kernel is closed")
            agent = self._agents.get(spec)
            if agent is None or agent.closed:
                key = spec.effective_memory_key
                for other, live in self._agents.items():
                    if other.effective_memory_key == key and not live.closed:
                        logger.warning(
                            "kernel: agents {!r} and {!r} share memory key {!r} (memory "
                            "and default session); set AgentSpec.memory_key to separate "
                            "them", other.name, spec.name, key,
                        )
                        shared = (key, other.name, spec.name)
                        break
                agent = Agent(self, spec)
                self._agents[spec] = agent
        if shared is not None:
            from nanobot.kernel.trace import safe_emit

            safe_emit(self._tracer, {
                "event": "kernel.memory_key_shared",
                "memory_key": shared[0],
                "agents": [shared[1], shared[2]],
            })
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
            if self._sessions is None and self._read_only:
                from nanobot.session.sqlite_store import ReadOnlySessionManager

                paths = self._env.paths
                self._sessions = ReadOnlySessionManager(  # type: ignore[assignment]
                    paths.work_dir, sessions_root=paths.sessions_root,
                )
            if self._sessions is None:
                from nanobot.session.manager import SessionManager
                from nanobot.session.sqlite_store import SqliteSessionStore

                paths = self._env.paths
                store = SqliteSessionStore(paths.work_dir, sessions_root=paths.sessions_root)
                self._sessions = SessionManager(
                    paths.work_dir, sessions_root=paths.sessions_root, store=store,
                )
                self._sessions.add_history_reset_observer(self._drop_session_history)
            return self._sessions

    def _drop_session_history(self, key: str, generation: int | None = None) -> None:
        """Drop *key*'s archived memory history from every agent's memory dir.

        Runs when a rewind leaves no committed summary (only the current history
        *generation*), a fork creates or replaces *key*, or *key* is deleted (every
        generation; ``SessionManager.add_history_reset_observer``): those entries
        summarise messages the transcript no longer has, and would otherwise come
        back as "Recent History". Built agents' stores are rewritten
        under their append lock; other agents' files (built in an earlier process)
        directly.
        """
        from nanobot.agent.memory import drop_session_history
        from nanobot.kernel.agent import AGENTS_DIRNAME

        with self._agents_lock:
            agents = list(self._agents.values())
        live = {}
        for agent in agents:
            loop = agent._loop
            if loop is not None:
                store = loop.context.memory
                live[store.history_file] = store
        root = self._env.paths.state_dir / AGENTS_DIRNAME
        for history in sorted(root.glob("*/memory/history.jsonl")):
            store = live.get(history)
            if store is not None:
                store.drop_session_history(key, generation=generation)
            else:
                drop_session_history(history, key, generation=generation)

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
            self._usage._close()  # noqa: SLF001
            self._close_action_pool()
            self._close_stores()
            self._close_llm()
            self._flush_budget()
            self._bridge.stop()
            # Only after stop() returned: a failed stop leaves the kernel open
            # so a later close() can retry.
            self._closed = True
            # Last: the state dir is free for the next writer once nothing writes.
            self._release_instance_lock()

    def _flush_budget(self) -> None:
        """Write a shared budget's pending settles/releases (``SharedCapBudget.flush``)."""
        flush = getattr(self._budget, "flush", None)
        if callable(flush):
            try:
                flush()
            except Exception as exc:  # noqa: BLE001 - closing must not fail the kernel close
                logger.warning("kernel: flushing the budget failed: {!r}", exc)
        detach = getattr(self._budget, "_detach_trace", None)
        if callable(detach):
            try:
                detach(self._tracer)
            except Exception:  # noqa: BLE001
                pass

    def _action_executor(self) -> ThreadPoolExecutor:
        """The pool sync host actions run on (see ``function_tool.ACTION_EXECUTOR``)."""
        with self._agents_lock:
            if self._action_pool is None:
                self._action_pool = ThreadPoolExecutor(
                    max_workers=self._action_workers, thread_name_prefix="moeka-action",
                )
            return self._action_pool

    def _close_action_pool(self) -> None:
        # After the agents: no run can submit another action. A cancelled run's
        # action thread may still be running; it is not waited for.
        with self._agents_lock:
            pool, self._action_pool = self._action_pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

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

    def _close_stores(self) -> None:
        # After the agents (they pin their memory stores); plain SQLite, no loop needed.
        with self._agents_lock:
            registry, self._memory_registry = self._memory_registry, None
            epi, self._epistemics = self._epistemics, None
        for name, closer in (("document stores", registry), ("epistemics", epi)):
            if closer is None:
                continue
            try:
                closer.close()
            except Exception as exc:  # noqa: BLE001 - closing must not fail the kernel close
                logger.warning("kernel: closing {} failed: {!r}", name, exc)

    @staticmethod
    async def _aclose_agents(agents: list[Agent]) -> None:
        for agent in agents:
            try:
                await agent._aclose()
            except BaseException as exc:  # noqa: BLE001 - close the others regardless
                logger.warning("kernel: closing agent {!r} failed: {!r}", agent.spec.name, exc)

    def _close_llm(self) -> None:
        # From here on the LLM builds no pool provider (one built after its close
        # would never be closed); a host holding ``kernel.llm`` gets RuntimeError.
        self._llm._refuse_new_providers()
        # Pool providers' HTTP clients live on the loop thread: close them there,
        # before it stops. Never started = no client was ever used.
        if self._bridge._state == "running":
            try:
                self._bridge.run(self._llm._aclose(), timeout=10.0)
            except Exception as exc:  # noqa: BLE001 - closing must not fail the kernel close
                logger.warning("kernel: closing LLM providers failed: {!r}", exc)
        # The cost ledger's usage store (shared by pool and host-registered
        # providers, and by other kernels on this state_dir): the last holder's
        # release closes it; it reopens lazily if a host-owned provider records again.
        from nanobot.llm_usage import release_llm_usage_store

        if self._holds_usage_store:
            self._holds_usage_store = False  # a retried close() releases once
            try:
                release_llm_usage_store(data_dir=self._core_env.paths.data_dir)
            except Exception as exc:  # noqa: BLE001
                logger.warning("kernel: closing the LLM usage store failed: {!r}", exc)

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
        if self._read_only:
            state += ", read-only"
        return f"Kernel({self._env!r}, {state})"


__all__ = ["Kernel"]
