"""Kernel LLM layer: ``generate`` / ``complete`` / ``complete_json`` / ``stream`` / ``batch``.

One :class:`LLM` per :class:`~nanobot.kernel.kernel.Kernel` (``kernel.llm``). It
keeps a provider pool keyed by model alias (a ``ModelSpec`` name): each alias gets
one provider, built on first use by the provider factory with the kernel's
environment, so every call is metered by the cost ledger. Hosts may inject their
own provider for an alias with :meth:`LLM.register_provider`.

Rules every entry point follows:

- Messages go to the provider exactly as given (``generate`` never rewrites them).
- A failed call raises a typed :class:`~nanobot.kernel.llm_errors.LLMError`; error
  text is never returned as content.
- All provider I/O runs on the kernel's loop thread, where the provider clients
  live; the async methods hop onto it from the caller's loop, and the ``*_sync``
  twins block on it from any thread (including inside a running event loop).
- ``timeout_s`` is a whole-call deadline: on expiry the provider call is cancelled
  and :class:`~nanobot.kernel.llm_errors.LLMTimeoutError` is raised.

Each logical call gets a ``call_id``; the ledger's ``model.call`` events for all of
its physical attempts carry that id, the alias, the attempt number and the call's
effective tags (bound through :func:`nanobot.kernel.ledger.call_attribution`): the
active trace span's tags with ``GenerateOptions.tags`` merged over them. The LLM
layer never opens spans itself.

With a kernel ``cache`` (and ``opts.cache``), a logical call is first looked up by
:meth:`LLM.request_key`; a hit makes no provider call and no budget admission.
With a kernel ``budget``, a miss is then admitted once against its worst-case
:class:`~nanobot.kernel.budget.CallEstimate` before anything is sent, settled per
physical attempt, and released when it ends (see :mod:`nanobot.kernel.budget`).
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import re
import threading
import time
import uuid
from collections.abc import (
    AsyncIterator,
    Callable,
    Generator,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal

from loguru import logger

from nanobot.kernel.budget import (
    BudgetedProvider,
    CallEstimate,
    Estimator,
    Metering,
    build_estimate,
    prompt_tokens_of,
)
from nanobot.kernel.frozen import FrozenMap, thaw
from nanobot.kernel.hostenv import ModelSpec
from nanobot.kernel.ledger import (
    CallAttribution,
    ModelPricing,
    UsageSource,
    call_attribution,
    compute_cost,
)
from nanobot.kernel.llm_errors import (
    AuthError,
    BudgetExceeded,
    LLMError,
    LLMTimeoutError,
    ModelNotFound,
    ParseError,
    QuotaError,
    RateLimitError,
    TruncatedError,
    classify,
)
from nanobot.kernel.messages import ImageInput, user_content
from nanobot.kernel.sampling import Sampling
from nanobot.kernel.trace import current_tags, safe_emit

if TYPE_CHECKING:
    from nanobot.kernel.kernel import Kernel
    from nanobot.kernel.ledger import PricingTable
    from nanobot.kernel.solvers import SolverRegistry
    from nanobot.providers.base import LLMProvider, LLMResponse, LLMUsage


# -- options and results ------------------------------------------------------


@dataclass(frozen=True)
class GenerateOptions:
    """Per-call options. Frozen and hashable (mappings are stored as ``FrozenMap``).

    - ``model``: a ``ModelSpec`` alias, or a raw model id sent to the default
      model's provider; ``None`` is the environment's default model.
    - ``sampling``: explicit sampling; set fields win over the model's defaults.
    - ``response_format`` / ``extra_body``: native structured output and extra
      request-body fields (see ``RequestExtras``).
    - ``timeout_s``: whole-call deadline (all retries included).
    - ``attempts``: cap on physical attempts for transient failures (``1`` = no retry).
    - ``cache``: whether a response cache may serve this call (used from Task 6).
    - ``affinity_key``: stable routing key (provider ``session_id``) that keeps a
      provider-side prefix cache warm across related calls.
    - ``tags``: host labels copied onto every ledger event of the call (merged over
      the active trace span's tags; these win).
    - ``on_unsupported``: ``"drop"`` (report and omit) or ``"raise"`` for request
      fields the provider cannot honour.
    """

    model: str | None = None
    sampling: Sampling = field(default_factory=Sampling)
    response_format: Mapping[str, Any] | None = None
    extra_body: Mapping[str, Any] | None = None
    timeout_s: float | None = None
    attempts: int | None = None
    cache: bool = True
    affinity_key: str | None = None
    tags: Mapping[str, Any] = field(default_factory=FrozenMap)
    on_unsupported: Literal["drop", "raise"] = "drop"

    def __post_init__(self) -> None:
        if self.sampling is None:
            object.__setattr__(self, "sampling", Sampling())
        for name in ("response_format", "extra_body"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, FrozenMap):
                object.__setattr__(self, name, FrozenMap(value))
        if not isinstance(self.tags, FrozenMap):
            object.__setattr__(self, "tags", FrozenMap(self.tags or {}))
        if self.timeout_s is not None and self.timeout_s <= 0:
            raise ValueError(f"timeout_s must be positive, got {self.timeout_s!r}")
        if self.attempts is not None and self.attempts < 1:
            raise ValueError(f"attempts must be at least 1, got {self.attempts!r}")
        if self.on_unsupported not in ("drop", "raise"):
            raise ValueError(f"on_unsupported must be 'drop' or 'raise', got {self.on_unsupported!r}")


def _combine_source(left: UsageSource, right: UsageSource) -> UsageSource:
    if left == right or right == "none":
        return left
    if left == "none":
        return right
    return "mixed"


@dataclass(frozen=True)
class Usage:
    """Token usage of one completion. ``input_tokens`` includes cache reads/writes."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    source: UsageSource = "none"

    @classmethod
    def from_llm_usage(cls, usage: LLMUsage | None) -> Usage:
        if usage is None:
            return cls()
        return cls(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens or 0,
            cache_write_tokens=usage.cache_write_tokens or 0,
            source=usage.source,
        )

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            source=_combine_source(self.source, other.source),
        )


@dataclass(frozen=True)
class Completion:
    """The result of one logical model call.

    ``attempts`` counts physical provider calls for ``generate``/``stream`` and
    request rounds for ``complete_json`` (0 when a deterministic solver answered).
    ``cost_usd`` is ``None`` when the price is unknown. ``truncated`` is True when
    the output hit the token limit.
    """

    text: str
    parsed: Any = None
    finish_reason: str = "stop"
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    provider: str = ""
    alias: str | None = None
    call_id: str = ""
    attempts: int = 1
    cached: bool = False
    cost_usd: float | None = None
    latency_ms: float = 0.0
    reasoning: str | None = None

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"


@dataclass(frozen=True)
class Request:
    """One logical call for :meth:`LLM.batch` / :meth:`LLM.estimate` / :meth:`LLM.request_key`.

    A request with a ``schema`` or ``model_cls`` is a JSON request (run like
    ``complete_json`` over *messages*, re-prompted up to ``retries`` times);
    otherwise it is a plain ``generate``. ``messages`` is stored as a tuple.
    """

    messages: Sequence[Mapping[str, Any]]
    opts: GenerateOptions | None = None
    schema: Mapping[str, Any] | None = None
    model_cls: type | None = None
    retries: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.messages, tuple):
            object.__setattr__(self, "messages", tuple(self.messages))
        if self.retries < 0:
            raise ValueError(f"retries must be >= 0, got {self.retries!r}")

    @property
    def is_json(self) -> bool:
        return self.schema is not None or self.model_cls is not None


@dataclass(frozen=True)
class BatchResult:
    """The outcome of :meth:`LLM.batch`.

    ``outcomes`` is in input order: a :class:`Completion` or the item's
    :class:`LLMError`. ``systemic`` is the error that aborted the batch (auth,
    quota or budget), in which case every unfinished item's outcome is that error.
    """

    outcomes: list[Completion | LLMError]
    systemic: LLMError | None = None

    @property
    def completions(self) -> list[Completion]:
        return [o for o in self.outcomes if isinstance(o, Completion)]

    @property
    def errors(self) -> list[LLMError]:
        return [o for o in self.outcomes if isinstance(o, LLMError)]


# -- shared JSON / provider helpers (also used by nanobot.api.complete) --------

_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)


def extract_json_text(text: str) -> str:
    """Best-effort extraction of the JSON payload from a model reply.

    Order: fenced ```json block, then the outermost {...} or [...] span,
    then the raw text (let json.loads produce the error).
    """
    fenced = _FENCE_RE.search(text)
    if fenced:
        return fenced.group(1).strip()
    stripped = text.strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start = stripped.find(opener)
        end = stripped.rfind(closer)
        if start != -1 and end > start:
            return stripped[start:end + 1]
    return stripped


def json_system_suffix(schema: Mapping[str, Any] | None) -> str:
    base = (
        "Respond ONLY with valid JSON — no prose, no markdown fences, "
        "no explanations before or after."
    )
    if schema is not None:
        base += " The JSON must match this JSON Schema:\n" + json.dumps(schema, indent=2)
    return base


def json_response_format(schema: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Native provider structured-output request for a JSON Schema.

    OpenAI/OpenRouter ``response_format`` — ``strict: false`` so providers whose
    models don't do strict schema validation still accept the request (a hard
    rejection falls back to the reprompt loop). Returns ``None`` without a
    schema — no native mode, keep the prompt suffix path.
    """
    if not schema:
        return None
    return {
        "type": "json_schema",
        "json_schema": {"name": "response", "schema": dict(schema), "strict": False},
    }


UNSOLVED = object()


def coerce_json(parsed: Any, model_cls: type | None) -> Any:
    """The one validation path for a JSON value, from an LLM reply or a solver."""
    if model_cls is not None:
        return model_cls.model_validate(parsed)  # type: ignore[attr-defined]
    return parsed


def solve_deterministic(
    task_type: str,
    task_payload: dict[str, Any] | None,
    prompt: str,
    model_cls: type | None,
    registry: SolverRegistry | None = None,
) -> tuple[Any, str | None]:
    """Solver fast path: ``(coerced value, solver name)``, or ``(UNSOLVED, None)``.

    *registry* is the registry to consult (a kernel's ``kernel.solvers``); ``None``
    is the process-wide default registry.
    """
    from nanobot.kernel import solvers

    payload = task_payload if task_payload is not None else {"prompt": prompt}
    solved = (
        registry.try_solve(task_type, payload) if registry is not None
        else solvers.try_solve(task_type, payload)
    )
    if solved is None:
        return UNSOLVED, None
    try:
        return coerce_json(solved.value, model_cls), solved.solver_name
    except Exception as exc:
        logger.warning(
            "solver {} ({}) value failed validation: {}; falling through to the LLM",
            solved.solver_name, task_type, exc,
        )
        return UNSOLVED, None


def try_deterministic(
    task_type: str,
    task_payload: dict[str, Any] | None,
    prompt: str,
    model_cls: type | None,
) -> Any:
    """Solver fast path: the coerced value, or ``UNSOLVED`` to fall through to the LLM."""
    return solve_deterministic(task_type, task_payload, prompt, model_cls)[0]


async def aclose_provider(provider: Any) -> None:
    """Best-effort close of a provider's async HTTP client, inside the live loop.

    A client left to the garbage collector tries to close its connection pool
    against a dead loop ("Event loop is closed" tracebacks). A routing wrapper
    (``FallbackProvider``) also has its primary closed. Never raises.
    """
    primary = getattr(provider, "_primary", None)
    if primary is not None and primary is not provider:
        await aclose_provider(primary)
    targets = [getattr(provider, attr, None) for attr in ("aclose", "close")]
    client = getattr(provider, "_client", None)
    if client is not None:
        targets += [getattr(client, attr, None) for attr in ("close", "aclose")]
    for fn in targets:
        if not callable(fn):
            continue
        try:
            res = fn()
            if inspect.isawaitable(res):
                await res
        except Exception:  # noqa: BLE001 — teardown must never break a call
            pass
        return


# -- the engine -----------------------------------------------------------------

# Error kinds that mean "the provider rejected the request shape" (e.g. an
# unsupported ``response_format``): complete_json falls back to prompt mode.
_REJECTION_KINDS = frozenset({"invalid_request", "unknown", "unsupported"})


@dataclass(frozen=True)
class _Route:
    alias: str | None
    spec: ModelSpec | None
    provider: LLMProvider
    model: str


@dataclass(frozen=True)
class _Plan:
    """A logical call resolved against the kernel: route, first-round messages, rounds."""

    kind: Literal["generate", "json"]
    route: _Route
    opts: GenerateOptions
    source: tuple[Mapping[str, Any], ...]  # the caller's messages, as given
    messages: list[dict[str, Any]]  # the first round as sent (JSON: system suffix added)
    native: Mapping[str, Any] | None  # the response_format of the first round
    schema: Mapping[str, Any] | None = None
    model_cls: type | None = None
    retries: int = 0

    @property
    def rounds(self) -> int:
        if self.kind != "json":
            return 1
        # Every parse retry, plus a native round the provider may reject.
        return 1 + self.retries + (1 if self.native is not None else 0)


def _new_call_id() -> str:
    return uuid.uuid4().hex


def _with_json_system(
    messages: Sequence[Mapping[str, Any]], suffix: str,
) -> list[dict[str, Any]]:
    """*messages* with the JSON instructions appended to (or added as) the system prompt."""
    out = [dict(message) for message in messages]
    if out and out[0].get("role") == "system" and isinstance(out[0].get("content"), str):
        content = out[0]["content"]
        out[0]["content"] = f"{content}\n\n{suffix}" if content else suffix
        return out
    return [{"role": "system", "content": suffix}, *out]


def _canonical_default(value: Any) -> Any:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=repr)
    if isinstance(value, (bytes, bytearray)):
        return hashlib.sha256(bytes(value)).hexdigest()
    return repr(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        default=_canonical_default,
    )


class _RateGate:
    """Holds new batch dispatches until a shared resume time (rate-limit pauses)."""

    def __init__(self) -> None:
        self._resume_at = 0.0

    def pause(self, seconds: float) -> None:
        loop = asyncio.get_running_loop()
        self._resume_at = max(self._resume_at, loop.time() + max(0.0, seconds))

    async def wait(self) -> None:
        loop = asyncio.get_running_loop()
        while (remaining := self._resume_at - loop.time()) > 0:
            await asyncio.sleep(remaining)


# Systemic errors abort a whole batch: retrying other items cannot succeed.
_SYSTEMIC_ERRORS: tuple[type[LLMError], ...] = (AuthError, QuotaError, BudgetExceeded)
_BATCH_RATE_LIMIT_RETRIES = 3
_DEFAULT_RETRY_AFTER_S = 1.0


def _spec_pricing(spec: ModelSpec) -> ModelPricing | None:
    values = (spec.tier, spec.price_in, spec.price_out, spec.price_cache_read)
    if all(value is None for value in values):
        return None
    return ModelPricing(*values)


class LLM:
    """The kernel's model-call engine (``kernel.llm``). See the module docstring."""

    def __init__(self, kernel: Kernel) -> None:
        self._kernel = kernel
        self._lock = threading.Lock()
        self._pool: dict[str, LLMProvider] = {}
        self._injected: dict[str, tuple[LLMProvider, ModelSpec]] = {}
        # id(provider) -> (provider, its ledger pricing table): one observer per
        # injected provider object, priced for every alias registered on it.
        self._ledgers: dict[int, tuple[LLMProvider, PricingTable]] = {}
        budget = kernel.budget
        self._metering: Metering | None = (
            Metering(budget, kernel.trace) if budget is not None else None
        )
        # alias -> the BudgetedProvider around that alias's provider (with a budget).
        self._wrapped: dict[str, BudgetedProvider] = {}
        # Set by the kernel's close: no new pool provider after that.
        self._closed = False

    # -- provider pool -----------------------------------------------------

    def register_provider(self, alias: str, provider: LLMProvider, spec: ModelSpec) -> None:
        """Serve *alias* with a host-built provider (tests, custom endpoints).

        The provider gets the kernel's trace sink and the cost ledger, priced from
        every spec registered on that provider object (one pricing table per
        provider, keyed ``(provider, model)``), so it is metered like a pool
        provider. The spec's sampling defaults are sent per request, exactly as
        for pool providers; the provider object itself is not reconfigured, so one
        provider may serve several aliases. It takes precedence over an
        ``env.models`` entry of the same name. The host keeps ownership: the
        kernel does not close it.
        """
        from nanobot.kernel.ledger import PricingTable
        from nanobot.providers.factory import attach_ledger

        if not isinstance(spec, ModelSpec):
            raise TypeError(f"spec must be a ModelSpec, got {type(spec).__name__}")
        pricing = _spec_pricing(spec)
        with self._lock:
            entry = self._ledgers.get(id(provider))
            if entry is None:
                table = PricingTable()
                self._ledgers[id(provider)] = (provider, table)
                provider.trace_sink = self._kernel.trace
                attach_ledger(provider, self._kernel.core_env, table)
            else:
                table = entry[1]
            if pricing is not None:
                table.add((provider.provider_name, spec.model), pricing)
            self._injected[alias] = (provider, spec)

    def _spec_for(self, alias: str) -> ModelSpec | None:
        injected = self._injected.get(alias)
        if injected is not None:
            return injected[1]
        return self._kernel.env.models.get(alias)

    def _provider_for(self, alias: str) -> LLMProvider:
        """The provider serving *alias*; wrapped in a ``BudgetedProvider`` under a budget."""
        raw = self._raw_provider_for(alias)
        metering = self._metering
        if metering is None:
            return raw
        with self._lock:
            wrapped = self._wrapped.get(alias)
            if wrapped is None or wrapped.inner is not raw:
                wrapped = BudgetedProvider(
                    raw, metering, self._direct_estimator(alias, raw), alias=alias,
                )
                self._wrapped[alias] = wrapped
            return wrapped

    def _raw_provider_for(self, alias: str) -> LLMProvider:
        with self._lock:
            injected = self._injected.get(alias)
            if injected is not None:
                return injected[0]
            provider = self._pool.get(alias)
            if provider is not None:
                return provider
            if self._closed:
                raise RuntimeError("kernel is closed")
            from nanobot.providers.factory import make_provider

            env = self._kernel.env
            try:
                provider = make_provider(
                    env.config, preset_name=alias, env=self._kernel.core_env,
                )
            except Exception as exc:
                raise ModelNotFound(
                    f"no provider can serve model {alias!r}: {exc}", model=alias,
                ) from exc
            self._pool[alias] = provider
            return provider

    def _route(self, model: str | None) -> _Route:
        env = self._kernel.env
        alias = env.default_model if model is None else model
        spec = self._spec_for(alias)
        if spec is not None:
            return _Route(alias, spec, self._provider_for(alias), spec.model)
        # Not an alias: a raw model id on the default model's provider.
        default = env.default_model
        if self._spec_for(default) is None:
            raise ModelNotFound(f"unknown model {alias!r} and no default model", model=alias)
        return _Route(None, None, self._provider_for(default), alias)

    def _refuse_new_providers(self) -> None:
        with self._lock:
            self._closed = True

    async def _aclose(self) -> None:
        """Close the pool's providers (on the loop their clients live on)."""
        with self._lock:
            self._closed = True
            providers = list(self._pool.values())
            self._pool.clear()
        for provider in providers:
            await aclose_provider(provider)

    # -- loop plumbing -----------------------------------------------------------

    async def _on_loop(self, coro: Any) -> Any:
        """Await *coro* on the kernel loop thread (directly when already on it)."""
        bridge = self._kernel._bridge
        if threading.current_thread() is bridge._thread:
            return await coro
        return await asyncio.wrap_future(bridge.submit(coro))

    def _run_sync(self, coro: Any) -> Any:
        return self._kernel._bridge.run(coro)

    # -- request building ----------------------------------------------------------

    @staticmethod
    def _default_sampling(route: _Route) -> Sampling | None:
        """The model's sampling defaults (``ModelSpec.sampling`` plus ``max_tokens``).

        Sent per request under the call's explicit sampling, for pool and
        injected providers alike; providers drop what they cannot honour quietly.
        """
        spec = route.spec
        if spec is None:
            return None
        defaults = spec.sampling or Sampling()
        if spec.max_tokens is not None:
            defaults = replace(defaults, max_tokens=spec.max_tokens)
        return defaults if defaults.set_fields() else None

    def _context(
        self, route: _Route, opts: GenerateOptions, response_format: Mapping[str, Any] | None,
    ) -> Any:
        from nanobot.providers.base import ProviderCallContext, RequestExtras

        return ProviderCallContext(
            session_id=opts.affinity_key,
            request=RequestExtras(
                response_format=thaw(response_format) if response_format else None,
                sampling=opts.sampling if opts.sampling.set_fields() else None,
                default_sampling=self._default_sampling(route),
                extra_body=thaw(opts.extra_body) if opts.extra_body else None,
                on_unsupported=opts.on_unsupported,
                max_attempts=opts.attempts,
            ),
        )

    def _completion(
        self,
        response: LLMResponse,
        route: _Route,
        attribution: CallAttribution,
        started: float,
    ) -> Completion:
        cost = attribution.cost_usd
        if attribution.attempts == 0 and route.spec is not None:
            # No ledger observer fired (a provider without one): price from the spec.
            spec = route.spec
            cost = compute_cost(
                response.usage, spec.price_in, spec.price_out, spec.price_cache_read,
                tier=spec.tier,
            )
        return Completion(
            text=response.content or "",
            finish_reason=response.finish_reason,
            usage=Usage.from_llm_usage(response.usage),
            model=route.model,
            provider=route.provider.provider_name,
            alias=route.alias,
            call_id=attribution.call_id,
            attempts=max(1, attribution.attempts),
            cost_usd=cost,
            latency_ms=(time.monotonic() - started) * 1000.0,
            reasoning=response.reasoning_content,
        )

    def _raise_for(self, response: LLMResponse, route: _Route, call_id: str) -> None:
        error = classify(
            response, call_id=call_id, model=route.model, provider=route.provider.provider_name,
        )
        if error is not None:
            raise error

    def _timeout_error(
        self, opts: GenerateOptions, route: _Route, call_id: str,
    ) -> LLMTimeoutError:
        return LLMTimeoutError(
            f"model call exceeded timeout_s={opts.timeout_s}",
            call_id=call_id,
            model=route.model,
            provider=route.provider.provider_name,
        )

    # -- plans, estimates and request keys -----------------------------------------------

    def _plan(
        self,
        messages: Sequence[Mapping[str, Any]],
        opts: GenerateOptions,
        *,
        kind: Literal["generate", "json"] = "generate",
        schema: Mapping[str, Any] | None = None,
        model_cls: type | None = None,
        retries: int = 0,
    ) -> _Plan:
        opts = self._with_span_tags(opts)
        route = self._route(opts.model)
        source = tuple(messages)
        if kind == "generate":
            return _Plan(
                kind, route, opts, source, [dict(m) for m in source], opts.response_format,
                retries=retries,
            )
        if model_cls is not None:
            schema = model_cls.model_json_schema()  # type: ignore[attr-defined]
        native = opts.response_format
        if native is None and (route.spec is None or route.spec.native_json is not False):
            native = json_response_format(schema)
        return _Plan(
            kind, route, opts, source, _with_json_system(source, json_system_suffix(schema)),
            native, schema=schema, model_cls=model_cls, retries=max(retries, 0),
        )

    @staticmethod
    def _with_span_tags(opts: GenerateOptions) -> GenerateOptions:
        """*opts* with the active trace span's tags merged under ``opts.tags`` (opts win).

        The effective tags feed the budget estimate (``per_tag`` caps), the
        ``cache.hit`` event and the ledger's ``model.call`` events.
        """
        span_tags = current_tags()
        if not span_tags:
            return opts
        return replace(opts, tags={**span_tags, **opts.tags})

    def _plan_request(self, request: Request) -> _Plan:
        if not isinstance(request, Request):
            raise TypeError(f"expected a Request, got {type(request).__name__}")
        return self._plan(
            request.messages, request.opts or GenerateOptions(),
            kind="json" if request.is_json else "generate",
            schema=request.schema, model_cls=request.model_cls, retries=request.retries,
        )

    def _merged_sampling(self, route: _Route, opts: GenerateOptions) -> Sampling:
        """The model's sampling defaults with the call's explicit fields written over them."""
        base = self._default_sampling(route) or Sampling()
        explicit = opts.sampling
        return replace(base, **{name: getattr(explicit, name) for name in explicit.set_fields()})

    def _pricing_for(
        self, spec: ModelSpec | None, provider: LLMProvider, model: str,
    ) -> ModelPricing | None:
        """The spec's pricing for its own model, else the provider ledger's table."""
        if spec is not None and spec.model == model:
            pricing = _spec_pricing(spec)
            if pricing is not None:
                return pricing
        lookup = getattr(getattr(provider, "_llm_call_observer", None), "pricing_for", None)
        if callable(lookup):
            try:
                return lookup(provider.provider_name, model)
            except Exception:  # noqa: BLE001 - an unpriced estimate, never a failed call
                logger.exception("pricing lookup failed for {}", model)
        return None

    def _estimate_plan(self, plan: _Plan, call_id: str) -> CallEstimate:
        route = plan.route
        max_tokens = self._merged_sampling(route, plan.opts).max_tokens
        if max_tokens is None:
            max_tokens = route.provider.generation.max_tokens
        return build_estimate(
            call_id=call_id,
            alias=route.alias,
            model=route.model,
            provider=route.provider.provider_name,
            prompt_tokens=prompt_tokens_of(plan.messages),
            max_output_tokens=max_tokens,
            rounds=plan.rounds,
            reprompts=plan.retries if plan.kind == "json" else 0,
            pricing=self._pricing_for(route.spec, route.provider, route.model),
            tags=plan.opts.tags,
        )

    def _direct_estimator(self, alias: str, provider: LLMProvider) -> Estimator:
        """Estimates for provider calls made outside the LLM layer (``BudgetedProvider``)."""

        def estimate(kwargs: dict[str, Any]) -> CallEstimate:
            spec = self._spec_for(alias)
            model = kwargs.get("model") or (spec.model if spec else provider.get_default_model())
            max_tokens = kwargs.get("max_tokens")
            if not isinstance(max_tokens, int) or isinstance(max_tokens, bool):
                max_tokens = (spec.max_tokens if spec else None) or provider.generation.max_tokens
            messages = kwargs.get("messages") or []
            tools = kwargs.get("tools")
            return build_estimate(
                call_id=_new_call_id(),
                alias=alias,
                model=model,
                provider=provider.provider_name,
                prompt_tokens=prompt_tokens_of(messages, tools if isinstance(tools, list) else None),
                max_output_tokens=max_tokens,
                pricing=self._pricing_for(spec, provider, model),
                # The active span's tags (an agent run's): per_tag caps apply to it.
                tags=current_tags(),
            )

        return estimate

    def _key_for(self, plan: _Plan) -> str:
        route = plan.route
        sampling = self._merged_sampling(route, plan.opts)
        cls = plan.model_cls
        payload = {
            "v": 1,
            "kind": plan.kind,
            "messages": [thaw(m) for m in plan.source],
            "alias": route.alias,
            "model": route.model,
            "provider": route.provider.provider_name,
            "sampling": {name: thaw(getattr(sampling, name)) for name in sampling.set_fields()},
            "response_format": thaw(plan.native) if plan.native is not None else None,
            "extra_body": thaw(plan.opts.extra_body) if plan.opts.extra_body else None,
            "schema": thaw(plan.schema) if plan.schema is not None else None,
            "model_cls": f"{cls.__module__}.{cls.__qualname__}" if cls is not None else None,
            "retries": plan.retries,
        }
        return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()

    def estimate(self, request: Request) -> CallEstimate:
        """The worst case *request* can cost (what a budget would be asked to admit).

        Input tokens come from ``estimate_prompt_tokens`` (images at
        ``budget.IMAGE_TOKENS`` each, a JSON schema counted once); output is the
        resolved ``max_tokens`` (call, then model defaults, then the provider's).
        A JSON request may take ``1 + retries`` rounds, plus a native
        ``response_format`` round the provider may reject. ``worst_case_usd`` is
        ``None`` when the model has no price. Sends nothing.
        """
        return self._estimate_plan(self._plan_request(request), _new_call_id())

    def request_key(self, request: Request) -> str:
        """Stable sha256 cache key for *request*.

        Covers the kind, the messages, the resolved alias/model/provider, the
        sampling merged with the model's defaults, the response format, extra body,
        schema, ``model_cls`` and retries. Equal requests get equal keys.
        """
        return self._key_for(self._plan_request(request))

    # -- cache and budget ------------------------------------------------------------------

    def _cache_lookup(
        self, plan: _Plan, call_id: str, started: float,
    ) -> tuple[str | None, Completion | None]:
        """``(key, hit)``: the cache key (``None`` = not cacheable) and a cached completion."""
        cache = self._kernel.cache
        if cache is None or not plan.opts.cache:
            return None, None
        key = self._key_for(plan)
        try:
            hit = cache.get(key)
        except Exception:  # noqa: BLE001 - a broken cache is a miss, never a failed call
            logger.exception("response cache get failed")
            return key, None
        if hit is None:
            return key, None
        if not isinstance(hit, Completion):
            logger.warning("response cache returned {} (not a Completion); ignored", type(hit))
            return key, None
        safe_emit(self._kernel.trace, {
            "event": "cache.hit",
            "call_id": call_id,
            "key": key[:16],
            "alias": plan.route.alias,
            "model": hit.model,
            "tags": thaw(plan.opts.tags),
        })
        return key, replace(
            hit,
            call_id=call_id,
            cached=True,
            attempts=0,
            cost_usd=0.0,
            latency_ms=(time.monotonic() - started) * 1000.0,
        )

    def _cache_store(self, key: str | None, completion: Completion | None) -> None:
        cache = self._kernel.cache
        if key is None or cache is None or completion is None:
            return
        if completion.truncated or completion.cached or completion.finish_reason == "error":
            return
        try:
            cache.put(key, completion)
        except Exception:  # noqa: BLE001
            logger.exception("response cache put failed")

    @contextmanager
    def _admitted(
        self, estimate: CallEstimate,
    ) -> Generator[Callable[[Any], None] | None]:
        """Admit *estimate* against the budget (raises ``BudgetExceeded``); yield the
        settlement hook for the call's attribution; release the rest on exit."""
        metering = self._metering
        if metering is None:
            yield None
            return
        admission = metering.admit(estimate)
        try:
            yield admission.settle
        finally:
            admission.release()

    async def _run_plan(self, plan: _Plan) -> Completion:
        """One logical call: cache lookup, admission, the provider call(s), cache store."""
        started = time.monotonic()
        call_id = _new_call_id()
        key, hit = self._cache_lookup(plan, call_id, started)
        if hit is not None:
            return hit
        estimate = self._estimate_plan(plan, call_id)
        with self._admitted(estimate) as settle:
            attribution = CallAttribution(
                call_id=call_id, alias=plan.route.alias, tags=plan.opts.tags, on_event=settle,
            )
            if plan.kind == "json":
                completion = await self._json_call(plan, attribution, started)
            else:
                completion = await self._generate(
                    plan.messages, plan.opts, attribution=attribution, route=plan.route,
                )
        self._cache_store(key, completion)
        return completion

    # -- generate ------------------------------------------------------------------------

    async def _generate(
        self,
        messages: Sequence[Mapping[str, Any]],
        opts: GenerateOptions,
        *,
        attribution: CallAttribution | None = None,
        route: _Route | None = None,
    ) -> Completion:
        """One provider call (with its retry policy); no cache and no admission."""
        if route is None:
            route = self._route(opts.model)
        if attribution is None:
            attribution = CallAttribution(
                call_id=_new_call_id(), alias=route.alias, tags=opts.tags,
            )
        context = self._context(route, opts, opts.response_format)
        started = time.monotonic()
        deadline = asyncio.timeout(opts.timeout_s)
        try:
            with call_attribution(attribution):
                async with deadline:
                    response = await route.provider.chat_with_retry(
                        # A deep copy: the retry loop may strip images in place.
                        messages=copy.deepcopy(list(messages)),
                        model=route.model,
                        provider_context=context,
                    )
        except TimeoutError:
            if deadline.expired():
                raise self._timeout_error(opts, route, attribution.call_id) from None
            raise
        except LLMError as exc:
            if exc.call_id is None:
                exc.call_id = attribution.call_id
            raise
        self._raise_for(response, route, attribution.call_id)
        return self._completion(response, route, attribution, started)

    async def generate(
        self, messages: Sequence[Mapping[str, Any]], opts: GenerateOptions | None = None,
    ) -> Completion:
        """Send *messages* exactly as given; return the :class:`Completion`.

        Raises a typed ``LLMError`` on failure. A reply cut at the token limit
        is returned with ``truncated=True``, not raised.
        """
        return await self._on_loop(self._generate_call(messages, opts or GenerateOptions()))

    def generate_sync(
        self, messages: Sequence[Mapping[str, Any]], opts: GenerateOptions | None = None,
    ) -> Completion:
        return self._run_sync(self._generate_call(messages, opts or GenerateOptions()))

    async def _generate_call(
        self, messages: Sequence[Mapping[str, Any]], opts: GenerateOptions,
    ) -> Completion:
        return await self._run_plan(self._plan(messages, opts))

    # -- complete --------------------------------------------------------------------------

    @staticmethod
    def _prompt_messages(
        prompt: str, system: str | None, images: Iterable[ImageInput],
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_content(prompt, images)})
        return messages

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        images: Iterable[ImageInput] = (),
        opts: GenerateOptions | None = None,
    ) -> Completion:
        """One user turn (plus an optional system prompt and images)."""
        return await self.generate(self._prompt_messages(prompt, system, images), opts)

    def complete_sync(
        self,
        prompt: str,
        *,
        system: str | None = None,
        images: Iterable[ImageInput] = (),
        opts: GenerateOptions | None = None,
    ) -> Completion:
        return self.generate_sync(self._prompt_messages(prompt, system, images), opts)

    # -- complete_json ---------------------------------------------------------------------

    async def _complete_json(
        self,
        prompt: str,
        *,
        schema: Mapping[str, Any] | None,
        model_cls: type | None,
        retries: int,
        system: str | None,
        images: Iterable[ImageInput],
        opts: GenerateOptions,
        task_type: str | None,
        task_payload: dict[str, Any] | None,
    ) -> Completion:
        if task_type is not None:
            started = time.monotonic()
            value, solver = solve_deterministic(
                task_type, task_payload, prompt, model_cls, getattr(self._kernel, "solvers", None),
            )
            if solver is not None:  # the solver path is never cached nor admitted
                return self._solved(value, solver, opts, _new_call_id(), started)
        plan = self._plan(
            self._prompt_messages(prompt, system, images), opts,
            kind="json", schema=schema, model_cls=model_cls, retries=retries,
        )
        return await self._run_plan(plan)

    async def _json_call(
        self, plan: _Plan, attribution: CallAttribution, started: float,
    ) -> Completion:
        opts = plan.opts
        # One deadline for the whole call: every round (and a rejected native one)
        # shares timeout_s, so rounds themselves run without their own deadline.
        deadline = asyncio.timeout(opts.timeout_s)
        try:
            async with deadline:
                return await self._json_rounds(
                    plan, replace(opts, timeout_s=None), attribution, started,
                )
        except TimeoutError:
            if deadline.expired():
                raise self._timeout_error(opts, plan.route, attribution.call_id) from None
            raise

    async def _json_rounds(
        self,
        plan: _Plan,
        opts: GenerateOptions,
        attribution: CallAttribution,
        started: float,
    ) -> Completion:
        call_id = attribution.call_id
        route = plan.route
        native = plan.native
        use_native = native is not None
        rounds = 0
        parse_rounds = 0
        usage = Usage()
        costs: list[float | None] = []
        messages = plan.messages
        last_error = ""
        last: Completion | None = None
        while parse_rounds <= plan.retries:
            rounds += 1
            round_opts = replace(opts, response_format=native if use_native else None)
            try:
                reply = await self._generate(
                    messages, round_opts, attribution=attribution, route=route,
                )
            except LLMError as exc:
                if use_native and exc.kind in _REJECTION_KINDS:
                    # The provider rejected native structured output: prompt mode.
                    logger.debug("native response_format rejected ({}); falling back", exc)
                    use_native = False
                    continue
                raise
            parse_rounds += 1
            last = reply
            usage = usage + reply.usage
            costs.append(reply.cost_usd)
            try:
                parsed = coerce_json(json.loads(extract_json_text(reply.text)), plan.model_cls)
            except Exception as exc:  # json decode or pydantic validation
                if reply.truncated:
                    raise TruncatedError(
                        "JSON reply was cut at the token limit",
                        call_id=call_id, model=reply.model, provider=reply.provider,
                        raw=reply.text,
                    ) from exc
                last_error = str(exc)
                use_native = False  # native didn't help; reprompt in plain text mode
                # Only the latest bad reply is shown, so every re-prompt round
                # stays within one reply of the first round's size.
                messages = [
                    *plan.messages,
                    {"role": "assistant", "content": reply.text},
                    {"role": "user", "content": (
                        f"Your previous reply was not valid.\nError: {last_error}\n"
                        "Reply again with ONLY corrected valid JSON."
                    )},
                ]
                continue
            return replace(
                reply,
                parsed=parsed,
                usage=usage,
                attempts=rounds,
                cost_usd=None if any(c is None for c in costs) else sum(costs),  # type: ignore[arg-type]
                latency_ms=(time.monotonic() - started) * 1000.0,
            )
        raise ParseError(
            f"model did not produce valid JSON after {parse_rounds} attempt(s): {last_error}",
            attempts=rounds,
            call_id=call_id,
            model=last.model if last else route.model,
            provider=last.provider if last else route.provider.provider_name,
            raw=last.text if last else None,
        )

    def _solved(
        self, value: Any, solver: str, opts: GenerateOptions, call_id: str, started: float,
    ) -> Completion:
        dump = getattr(value, "model_dump_json", None)
        text = dump() if callable(dump) else json.dumps(value, default=str)
        return Completion(
            text=text,
            parsed=value,
            model=solver,
            provider="solver",
            alias=opts.model,
            call_id=call_id,
            attempts=0,
            cost_usd=0.0,
            latency_ms=(time.monotonic() - started) * 1000.0,
        )

    async def complete_json(
        self,
        prompt: str,
        *,
        schema: Mapping[str, Any] | None = None,
        model_cls: type | None = None,
        retries: int = 2,
        system: str | None = None,
        images: Iterable[ImageInput] = (),
        opts: GenerateOptions | None = None,
        task_type: str | None = None,
        task_payload: dict[str, Any] | None = None,
    ) -> Completion:
        """A JSON reply, parsed into ``Completion.parsed`` (a ``model_cls`` instance if given).

        A deterministic solver for *task_type* answers first with no provider call.
        Otherwise native ``response_format`` is tried first (unless the model's
        ``native_json`` is False); a provider rejection falls back to the prompt
        suffix. A reply that fails to parse is re-prompted with the error up to
        *retries* times (a rejected native round does not use one), then
        ``ParseError``; a truncated reply that fails to parse is ``TruncatedError``.
        ``Completion.attempts`` is the number of rounds; usage and cost are summed.
        The whole call is one cache lookup and one budget admission.
        """
        return await self._on_loop(self._complete_json(
            prompt, schema=schema, model_cls=model_cls, retries=retries, system=system,
            images=images, opts=opts or GenerateOptions(), task_type=task_type,
            task_payload=task_payload,
        ))

    def complete_json_sync(
        self,
        prompt: str,
        *,
        schema: Mapping[str, Any] | None = None,
        model_cls: type | None = None,
        retries: int = 2,
        system: str | None = None,
        images: Iterable[ImageInput] = (),
        opts: GenerateOptions | None = None,
        task_type: str | None = None,
        task_payload: dict[str, Any] | None = None,
    ) -> Completion:
        return self._run_sync(self._complete_json(
            prompt, schema=schema, model_cls=model_cls, retries=retries, system=system,
            images=images, opts=opts or GenerateOptions(), task_type=task_type,
            task_payload=task_payload,
        ))

    # -- stream ------------------------------------------------------------------------------

    def stream(
        self, messages: Sequence[Mapping[str, Any]], opts: GenerateOptions | None = None,
    ) -> TextStream:
        """Stream text deltas; ``await stream.completion()`` for the final result.

        Use it as ``async with`` (or call ``aclose()``) so an early exit cancels
        the provider call.
        """
        return TextStream(self, messages, opts or GenerateOptions())

    def stream_sync(
        self, messages: Sequence[Mapping[str, Any]], opts: GenerateOptions | None = None,
    ) -> SyncTextStream:
        """Sync :meth:`stream`: iterate deltas, then ``completion()``. Use ``with`` (or
        ``close()``) so an early exit cancels the provider call."""
        return SyncTextStream(TextStream(self, messages, opts or GenerateOptions()))

    async def _stream_deltas(
        self, messages: Sequence[Mapping[str, Any]], opts: GenerateOptions, out: _StreamResult,
    ) -> AsyncIterator[str]:
        """Runs on the kernel loop: yields deltas, then stores the completion on *out*.

        *out* is a plain holder, not the ``TextStream``: the generator must not
        reference its owner, or an abandoned stream would sit in a reference
        cycle (never finalised, provider task still running) until cyclic GC.
        """
        plan = self._plan(messages, opts)
        route = plan.route
        started = time.monotonic()
        call_id = _new_call_id()
        key, hit = self._cache_lookup(plan, call_id, started)
        if hit is not None:
            if hit.text:
                yield hit.text
            out.completion = hit
            return
        estimate = self._estimate_plan(plan, call_id)
        with self._admitted(estimate) as settle:
            attribution = CallAttribution(
                call_id=call_id, alias=route.alias, tags=plan.opts.tags, on_event=settle,
            )
            context = self._context(route, opts, opts.response_format)
            queue: asyncio.Queue[Any] = asyncio.Queue()
            done = object()
            deadline = asyncio.timeout(opts.timeout_s)

            async def on_delta(text: str) -> None:
                if text:
                    queue.put_nowait(text)

            async def call() -> LLMResponse:
                try:
                    async with deadline:
                        return await route.provider.chat_stream_with_retry(
                            messages=copy.deepcopy(plan.messages),
                            model=route.model,
                            provider_context=context,
                            on_content_delta=on_delta,
                        )
                finally:
                    queue.put_nowait(done)

            # The task copies the context now, so the ledger sees the attribution.
            with call_attribution(attribution):
                task = asyncio.create_task(call())
            try:
                while True:
                    item = await queue.get()
                    if item is done:
                        break
                    yield item
                try:
                    response = await task
                except TimeoutError:
                    if deadline.expired():
                        raise self._timeout_error(opts, route, attribution.call_id) from None
                    raise
                self._raise_for(response, route, attribution.call_id)
                out.completion = self._completion(response, route, attribution, started)
            finally:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except BaseException:  # noqa: BLE001 - cancelled on early exit
                        pass
        self._cache_store(key, out.completion)

    # -- batch ---------------------------------------------------------------------------------

    async def batch(
        self, requests: Iterable[Request], *, concurrency: int | None = None,
    ) -> BatchResult:
        """Run *requests* concurrently; outcomes come back in input order.

        At most *concurrency* (default: the kernel's ``max_concurrency``) calls
        are in flight. Each item is a full logical call (cache, budget, retries).

        - ``AuthError`` / ``QuotaError`` / ``BudgetExceeded`` are systemic: the
          remaining items are cancelled, every unfinished item's outcome is that
          error, and ``BatchResult.systemic`` is set.
        - ``RateLimitError`` pauses new dispatches for ``retry_after`` (1s when
          unknown) and retries that item, up to 3 times; after that the error is
          the item's outcome.
        - Any other ``LLMError`` is that item's outcome.
        """
        requests = list(requests)
        return await self._on_loop(self._batch(requests, concurrency))

    def batch_sync(
        self, requests: Iterable[Request], *, concurrency: int | None = None,
    ) -> BatchResult:
        return self._run_sync(self._batch(list(requests), concurrency))

    async def _run_request(self, request: Request) -> Completion:
        return await self._run_plan(self._plan_request(request))

    async def _batch(self, requests: list[Request], concurrency: int | None) -> BatchResult:
        for request in requests:
            if not isinstance(request, Request):
                raise TypeError(f"batch items must be Request, got {type(request).__name__}")
        limit = self._kernel.max_concurrency if concurrency is None else concurrency
        if limit < 1:
            raise ValueError(f"concurrency must be at least 1, got {limit!r}")
        semaphore = asyncio.Semaphore(limit)
        gate = _RateGate()
        outcomes: list[Completion | LLMError | None] = [None] * len(requests)
        systemic: list[LLMError] = []
        tasks: list[asyncio.Task[None]] = []

        def abort(error: LLMError, index: int) -> None:
            if systemic:
                return
            systemic.append(error)
            for other, task in enumerate(tasks):
                if other != index and not task.done():
                    task.cancel()

        async def run(index: int, request: Request) -> None:
            rate_limited = 0
            while True:
                async with semaphore:
                    await gate.wait()
                    if systemic:
                        return
                    try:
                        outcomes[index] = await self._run_request(request)
                        return
                    except RateLimitError as exc:
                        if rate_limited >= _BATCH_RATE_LIMIT_RETRIES:
                            outcomes[index] = exc
                            return
                        rate_limited += 1
                        wait = exc.retry_after
                        gate.pause(_DEFAULT_RETRY_AFTER_S if wait is None else wait)
                    except _SYSTEMIC_ERRORS as exc:
                        outcomes[index] = exc
                        abort(exc, index)
                        return
                    except LLMError as exc:
                        outcomes[index] = exc
                        return

        tasks.extend(asyncio.create_task(run(i, r)) for i, r in enumerate(requests))
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException) and not isinstance(
                result, asyncio.CancelledError,
            ):
                raise result  # a bug, not a model error: fail loudly
        error = systemic[0] if systemic else None
        final: list[Completion | LLMError] = []
        for outcome in outcomes:
            if outcome is None:
                if error is None:  # unreachable: every item ends with an outcome
                    raise RuntimeError("batch item finished without an outcome")
                outcome = error
            final.append(outcome)
        return BatchResult(outcomes=final, systemic=error)


class _StreamResult:
    """Where a stream's generator leaves its final :class:`Completion`."""

    __slots__ = ("completion",)

    def __init__(self) -> None:
        self.completion: Completion | None = None


class TextStream:
    """Async iterator of text deltas from :meth:`LLM.stream`.

    ``await completion()`` drains any remaining deltas and returns the final
    :class:`Completion` (or raises the call's typed error).
    """

    def __init__(
        self, llm: LLM, messages: Sequence[Mapping[str, Any]], opts: GenerateOptions,
    ) -> None:
        self._llm = llm
        self._result = _StreamResult()
        self._gen = llm._stream_deltas(messages, opts, self._result)
        self._finished = False

    def __aiter__(self) -> TextStream:
        return self

    async def __anext__(self) -> str:
        if self._finished:
            raise StopAsyncIteration
        gen = self._gen

        async def _next() -> str:
            return await gen.__anext__()

        try:
            return await self._llm._on_loop(_next())
        except BaseException:
            self._finished = True
            raise

    async def completion(self) -> Completion:
        async for _ in self:
            pass
        if self._result.completion is None:
            raise RuntimeError("stream was closed before it completed")
        return self._result.completion

    async def aclose(self) -> None:
        """Stop the stream early (cancels the provider call). Idempotent."""
        self._finished = True
        gen = self._gen

        async def _close() -> None:
            await gen.aclose()

        await self._llm._on_loop(_close())

    async def __aenter__(self) -> TextStream:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()


class SyncTextStream:
    """Sync iterator of text deltas from :meth:`LLM.stream_sync`."""

    def __init__(self, stream: TextStream) -> None:
        self._stream = stream
        self._iter: Iterator[str] = stream._llm._kernel._bridge.iterate(stream._gen)

    def __iter__(self) -> SyncTextStream:
        return self

    def __next__(self) -> str:
        return next(self._iter)

    def completion(self) -> Completion:
        for _ in self._iter:
            pass
        if self._stream._result.completion is None:
            raise RuntimeError("stream was closed before it completed")
        return self._stream._result.completion

    def close(self) -> None:
        """Stop the stream early (cancels the provider call). Idempotent."""
        close = getattr(self._iter, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> SyncTextStream:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


__all__ = [
    "LLM",
    "BatchResult",
    "Completion",
    "GenerateOptions",
    "Request",
    "SyncTextStream",
    "TextStream",
    "Usage",
]
