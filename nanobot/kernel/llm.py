"""Kernel LLM layer: ``generate`` / ``complete`` / ``complete_json`` / ``stream``.

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
its physical attempts carry that id, the alias, the attempt number and the host's
tags (bound through :func:`nanobot.kernel.ledger.call_attribution`).
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import re
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal

from loguru import logger

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
    LLMError,
    LLMTimeoutError,
    ModelNotFound,
    ParseError,
    TruncatedError,
    classify,
)
from nanobot.kernel.messages import ImageInput, user_content
from nanobot.kernel.sampling import Sampling

if TYPE_CHECKING:
    from nanobot.kernel.kernel import Kernel
    from nanobot.kernel.ledger import PricingTable
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
    - ``tags``: host labels copied onto every ledger event of the call.
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
) -> tuple[Any, str | None]:
    """Solver fast path: ``(coerced value, solver name)``, or ``(UNSOLVED, None)``."""
    from nanobot.kernel import solvers

    payload = task_payload if task_payload is not None else {"prompt": prompt}
    solved = solvers.try_solve(task_type, payload)
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


def _new_call_id() -> str:
    return uuid.uuid4().hex


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
        env = self._kernel.env
        pricing = _spec_pricing(spec)
        with self._lock:
            entry = self._ledgers.get(id(provider))
            if entry is None:
                table = PricingTable()
                self._ledgers[id(provider)] = (provider, table)
                provider.trace_sink = env.trace
                attach_ledger(provider, env.core, table)
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
        with self._lock:
            injected = self._injected.get(alias)
            if injected is not None:
                return injected[0]
            provider = self._pool.get(alias)
            if provider is not None:
                return provider
            from nanobot.providers.factory import make_provider

            env = self._kernel.env
            try:
                provider = make_provider(env.config, preset_name=alias, env=env.core)
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

    async def _aclose(self) -> None:
        """Close the pool's providers (on the loop their clients live on)."""
        with self._lock:
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

    # -- generate ------------------------------------------------------------------------

    async def _generate(
        self,
        messages: Sequence[Mapping[str, Any]],
        opts: GenerateOptions,
        *,
        attribution: CallAttribution | None = None,
    ) -> Completion:
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
        return await self._on_loop(self._generate(messages, opts or GenerateOptions()))

    def generate_sync(
        self, messages: Sequence[Mapping[str, Any]], opts: GenerateOptions | None = None,
    ) -> Completion:
        return self._run_sync(self._generate(messages, opts or GenerateOptions()))

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
        started = time.monotonic()
        call_id = _new_call_id()
        if task_type is not None:
            value, solver = solve_deterministic(task_type, task_payload, prompt, model_cls)
            if solver is not None:
                return self._solved(value, solver, opts, call_id, started)

        if model_cls is not None:
            schema = model_cls.model_json_schema()  # type: ignore[attr-defined]
        suffix = json_system_suffix(schema)
        full_system = f"{system}\n\n{suffix}" if system else suffix
        images = tuple(images)

        route = self._route(opts.model)
        native = opts.response_format
        if native is None and (route.spec is None or route.spec.native_json is not False):
            native = json_response_format(schema)
        attribution = CallAttribution(call_id=call_id, alias=route.alias, tags=opts.tags)
        # One deadline for the whole call: every round (and a rejected native one)
        # shares timeout_s, so rounds themselves run without their own deadline.
        deadline = asyncio.timeout(opts.timeout_s)
        try:
            async with deadline:
                return await self._json_rounds(
                    prompt, full_system=full_system, images=images, model_cls=model_cls,
                    retries=retries, opts=replace(opts, timeout_s=None), route=route,
                    native=native, attribution=attribution, started=started,
                )
        except TimeoutError:
            if deadline.expired():
                raise self._timeout_error(opts, route, call_id) from None
            raise

    async def _json_rounds(
        self,
        prompt: str,
        *,
        full_system: str,
        images: tuple[ImageInput, ...],
        model_cls: type | None,
        retries: int,
        opts: GenerateOptions,
        route: _Route,
        native: Mapping[str, Any] | None,
        attribution: CallAttribution,
        started: float,
    ) -> Completion:
        call_id = attribution.call_id
        use_native = native is not None
        rounds = 0
        parse_rounds = 0
        usage = Usage()
        costs: list[float | None] = []
        attempt_prompt = prompt
        last_error = ""
        last: Completion | None = None
        while parse_rounds <= max(retries, 0):
            rounds += 1
            messages = self._prompt_messages(attempt_prompt, full_system, images)
            round_opts = replace(opts, response_format=native if use_native else None)
            try:
                reply = await self._generate(messages, round_opts, attribution=attribution)
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
                parsed = coerce_json(json.loads(extract_json_text(reply.text)), model_cls)
            except Exception as exc:  # json decode or pydantic validation
                if reply.truncated:
                    raise TruncatedError(
                        "JSON reply was cut at the token limit",
                        call_id=call_id, model=reply.model, provider=reply.provider,
                        raw=reply.text,
                    ) from exc
                last_error = str(exc)
                use_native = False  # native didn't help; reprompt in plain text mode
                attempt_prompt = (
                    f"{prompt}\n\n"
                    f"Your previous reply was not valid:\n{reply.text}\n\n"
                    f"Error: {last_error}\n"
                    "Reply again with ONLY corrected valid JSON."
                )
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
        route = self._route(opts.model)
        attribution = CallAttribution(call_id=_new_call_id(), alias=route.alias, tags=opts.tags)
        context = self._context(route, opts, opts.response_format)
        started = time.monotonic()
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
                        messages=copy.deepcopy(list(messages)),
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
    "Completion",
    "GenerateOptions",
    "SyncTextStream",
    "TextStream",
    "Usage",
]
