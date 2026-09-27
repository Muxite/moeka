"""Programmatic one-shot LLM completion — a small, stable API surface.

This lets external services (e.g. awork) reuse moeka's provider layer — model
presets, multi-provider routing, and inline fallback — without running the full
agent loop. It is additive and has no effect on the gateway: nothing here is
imported by the running dispatcher, and all heavy imports are lazy.

Usage::

    from nanobot.api import complete
    text = complete("Summarize this README", system="Be terse.")

    # async, inside an event loop:
    from nanobot.api import acomplete
    text = await acomplete("...", model="anthropic/claude-opus-4-5")

Config defaults to ``~/.nanobot/config.json`` (same as the gateway), so provider
keys and the active model/preset come from the user's existing moeka setup.
"""
from __future__ import annotations

import asyncio
import base64
import dataclasses
import inspect
import json
import mimetypes
import re
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any


async def _aclose_provider(provider: Any) -> None:
    """Best-effort close of a provider's async HTTP client.

    The sync wrappers run each completion via ``asyncio.run``, which closes the
    loop as soon as the coroutine returns. If the provider's underlying
    ``AsyncOpenAI`` / ``AsyncAnthropic`` client is left to be garbage-collected,
    its httpx connection pool tries to close against the now-dead loop and emits
    noisy ``RuntimeError: Event loop is closed`` / "Task exception was never
    retrieved" tracebacks. Closing it here — inside the live loop, in a
    ``finally`` — releases those connections cleanly. Never raises.
    """
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


def _usage_payload(model: str | None, usage: Any) -> dict[str, Any]:
    """Flatten provider ``LLMUsage`` into the plain dict a usage_sink expects."""
    fields = dataclasses.asdict(usage) if dataclasses.is_dataclass(usage) else dict(usage)
    return {"model": model, **fields}


def _report_usage(usage_sink: Any, model: str | None, response: Any) -> None:
    """Best-effort usage callback — metering must never break a completion."""
    usage = getattr(response, "usage", None)
    if usage_sink is None or usage is None:
        return
    try:
        usage_sink(_usage_payload(model, usage))
    except Exception:  # noqa: BLE001 — metering must not break inference
        pass


def _image_part(image: str | bytes | Path) -> dict[str, Any]:
    """Build an OpenAI-style image content part from a path, URL, or raw bytes.

    Accepts: an http(s)/data URL (passed through), a local file path, or raw
    image bytes. Local files and bytes are base64-encoded into a data URL.
    """
    if isinstance(image, (bytes, bytearray)):
        data = base64.b64encode(bytes(image)).decode()
        url = f"data:image/png;base64,{data}"
    elif isinstance(image, str) and image.startswith(("http://", "https://", "data:")):
        url = image
    else:
        path = Path(image)
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        data = base64.b64encode(path.read_bytes()).decode()
        url = f"data:{mime};base64,{data}"
    return {"type": "image_url", "image_url": {"url": url}}


def _user_content(prompt: str, images: list[str | bytes | Path] | None) -> Any:
    """Plain string when no images, else a multimodal content-part list."""
    if not images:
        return prompt
    return [{"type": "text", "text": prompt}] + [_image_part(i) for i in images]


async def acomplete(
    prompt: str,
    *,
    system: str | None = None,
    images: list[str | bytes | Path] | None = None,
    config: Any | None = None,
    config_dict: dict[str, Any] | None = None,
    config_path: str | Path | None = None,
    model: str | None = None,
    preset: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    usage_sink: Any | None = None,
    response_format: dict[str, Any] | None = None,
    env: Any | None = None,
) -> str:
    """Run a single chat completion through moeka's provider layer.

    Args:
        prompt: The user message.
        system: Optional system prompt.
        images: Optional images (paths, http/data URLs, or raw bytes) for
            vision-capable models. Sent as OpenAI-style multimodal content.
        config: A pre-built :class:`Config` (pure data; no file read).
        config_dict: A plain dict validated into a ``Config`` in memory.
        config_path: Path to ``config.json``; defaults to ``~/.nanobot/config.json``.
            Supply at most one of ``config`` / ``config_dict`` / ``config_path``.
        model: Override the resolved model (provider-specific id).
        preset: Named model preset to use instead of the active default.
        max_tokens / temperature: Generation overrides; ``None`` uses the
            provider's configured generation defaults.
        usage_sink: Optional callable invoked with
            ``{"model": <requested model or None>, **usage}`` after the call,
            when the provider reported usage. Best-effort — a sink error never
            breaks the completion. (No cost here: providers only return token
            counts; the caller prices them.)
        response_format: Optional native structured-output request (OpenAI/
            OpenRouter ``response_format`` shape), carried on the provider call
            context. Only providers that support it honour it; others ignore
            or reject the kwarg (see :func:`acomplete_json`'s fallback).
        env: Optional ``CoreEnvironment``. Given, the provider is built with it
            (host credentials, and the cost ledger measures the call). ``None``
            keeps the pre-kernel call exactly.

    Returns:
        The assistant's text content.
    """
    from nanobot.config.loader import config_from_sources
    from nanobot.providers.base import ProviderCallContext, RequestExtras
    from nanobot.providers.factory import make_provider

    resolved_config, _ = config_from_sources(
        config=config, config_dict=config_dict, config_path=config_path,
    )
    extra: dict[str, Any] = {"env": env} if env is not None else {}
    provider = make_provider(resolved_config, preset_name=preset, model=model, **extra)

    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": _user_content(prompt, images)})

    call_kwargs: dict[str, Any] = dict(
        messages=messages, max_tokens=max_tokens, temperature=temperature,
    )
    if response_format is not None:
        call_kwargs["provider_context"] = ProviderCallContext(
            request=RequestExtras(response_format=response_format)
        )
    try:
        response = await provider.chat_with_retry(**call_kwargs)
    finally:
        # Release the provider's async HTTP client inside this loop so the sync
        # ``asyncio.run`` wrapper doesn't leave it to be GC'd against a closed
        # loop (noisy "Event loop is closed" tracebacks).
        await _aclose_provider(provider)
    if response.finish_reason == "error":
        raise RuntimeError(f"moeka completion failed: {response.content}")
    if response.content is None:
        raise RuntimeError(
            "moeka completion returned no content "
            f"(finish_reason={response.finish_reason!r}, "
            f"error_type={response.error_type!r})"
        )
    _report_usage(usage_sink, model, response)
    return response.content


async def acomplete_stream(
    prompt: str,
    *,
    system: str | None = None,
    images: list[str | bytes | Path] | None = None,
    config: Any | None = None,
    config_dict: dict[str, Any] | None = None,
    config_path: str | Path | None = None,
    model: str | None = None,
    preset: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    env: Any | None = None,
) -> AsyncIterator[str]:
    """Stream a single chat completion as text chunks.

    Same arguments as :func:`acomplete`. Providers without native streaming
    deliver the full reply as one chunk. Raises ``RuntimeError`` when the
    provider reports an error.
    """
    from nanobot.config.loader import config_from_sources
    from nanobot.providers.factory import make_provider

    resolved_config, _ = config_from_sources(
        config=config, config_dict=config_dict, config_path=config_path,
    )
    extra: dict[str, Any] = {"env": env} if env is not None else {}
    provider = make_provider(resolved_config, preset_name=preset, model=model, **extra)

    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": _user_content(prompt, images)})

    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def _on_delta(chunk: str) -> None:
        await queue.put(chunk)

    async def _run() -> Any:
        try:
            return await provider.chat_stream_with_retry(
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                on_content_delta=_on_delta,
            )
        finally:
            await queue.put(None)

    task = asyncio.create_task(_run())
    try:
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            yield chunk
        response = await task
        if response.finish_reason == "error":
            raise RuntimeError(f"moeka streaming completion failed: {response.content}")
    finally:
        if not task.done():
            task.cancel()
        await _aclose_provider(provider)


def complete_stream(
    prompt: str,
    **kwargs: Any,
) -> Iterator[str]:
    """Synchronous streaming bridge for sync hosts (e.g. awork pipeline blocks).

    Runs :func:`acomplete_stream` in a worker thread with its own event loop
    and yields chunks as they arrive. Raises if called inside a running event
    loop — iterate :func:`acomplete_stream` there instead.
    """
    import queue as _queue
    import threading

    _reject_running_loop("complete_stream", "acomplete_stream")
    chunks: _queue.Queue[Any] = _queue.Queue()
    done = object()

    def _worker() -> None:
        async def _consume() -> None:
            async for chunk in acomplete_stream(prompt, **kwargs):
                chunks.put(chunk)

        try:
            asyncio.run(_consume())
            chunks.put(done)
        except BaseException as exc:  # surfaced to the consuming thread
            chunks.put(exc)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    while True:
        item = chunks.get()
        if item is done:
            break
        if isinstance(item, BaseException):
            raise item
        yield item


_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)


def _extract_json_text(text: str) -> str:
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


def _json_system_suffix(schema: dict[str, Any] | None) -> str:
    base = (
        "Respond ONLY with valid JSON — no prose, no markdown fences, "
        "no explanations before or after."
    )
    if schema is not None:
        base += " The JSON must match this JSON Schema:\n" + json.dumps(schema, indent=2)
    return base


def _json_response_format(schema: dict[str, Any] | None) -> dict[str, Any] | None:
    """Native provider structured-output request for a JSON Schema.

    OpenAI/OpenRouter ``response_format`` — ``strict: false`` so providers whose
    models don't do strict schema validation still accept the request (a hard
    rejection is caught by :func:`acomplete_json` and falls back to the reprompt
    loop). Returns ``None`` without a schema — no native mode, keep the prompt
    suffix path.
    """
    if not schema:
        return None
    return {
        "type": "json_schema",
        "json_schema": {"name": "response", "schema": schema, "strict": False},
    }


_UNSOLVED = object()


def _coerce_json(parsed: Any, model_cls: type | None) -> Any:
    """The one validation path for a JSON value, from an LLM reply or a solver."""
    if model_cls is not None:
        return model_cls.model_validate(parsed)
    return parsed


def _solve_deterministic(
    task_type: str,
    task_payload: dict[str, Any] | None,
    prompt: str,
    model_cls: type | None,
) -> tuple[Any, str | None]:
    """Solver fast path: ``(coerced value, solver name)``, or ``(_UNSOLVED, None)``."""
    from loguru import logger

    from nanobot.kernel import solvers

    payload = task_payload if task_payload is not None else {"prompt": prompt}
    solved = solvers.try_solve(task_type, payload)
    if solved is None:
        return _UNSOLVED, None
    try:
        return _coerce_json(solved.value, model_cls), solved.solver_name
    except Exception as exc:
        logger.warning(
            "solver {} ({}) value failed validation: {}; falling through to the LLM",
            solved.solver_name, task_type, exc,
        )
        return _UNSOLVED, None


def _try_deterministic(
    task_type: str,
    task_payload: dict[str, Any] | None,
    prompt: str,
    model_cls: type | None,
) -> Any:
    """Solver fast path: the coerced value, or ``_UNSOLVED`` to fall through to the LLM."""
    return _solve_deterministic(task_type, task_payload, prompt, model_cls)[0]


async def acomplete_json(
    prompt: str,
    *,
    schema: dict[str, Any] | None = None,
    model_cls: type | None = None,
    retries: int = 2,
    system: str | None = None,
    task_type: str | None = None,
    task_payload: dict[str, Any] | None = None,
    **kwargs: Any,
) -> Any:
    """One-shot completion constrained to JSON, with parse-retry.

    Provider-agnostic by design (no native JSON mode required): a schema-aware
    instruction is appended to the system prompt, the reply is parsed, and on
    a parse/validation failure the model is re-prompted with the error, up to
    *retries* extra attempts.

    Args:
        prompt: The user message.
        schema: Optional JSON Schema dict the reply must match (sent to the
            model as an instruction; validated only via ``model_cls``).
        model_cls: Optional pydantic ``BaseModel`` subclass. Its schema is
            derived automatically (overriding *schema*) and the parsed JSON is
            validated — the validated instance is returned.
        retries: Extra attempts after a failed parse/validation.
        system: Optional system prompt; the JSON instruction is appended.
        task_type: Optional deterministic-solver task type (I6). When a solver
            registered for it (:mod:`nanobot.kernel.solvers`) solves the
            payload, its value is returned after the same ``model_cls``
            validation an LLM reply gets, and no provider is built or called.
            An invalid solver value falls through to the LLM, as an invalid
            LLM reply leads to a retry. ``None`` (default) skips the lookup.
        task_payload: The dict handed to the solvers; defaults to
            ``{"prompt": prompt}``. Ignored without ``task_type``.
        **kwargs: Forwarded to :func:`acomplete` (``model``, ``preset``,
            ``images``, ``max_tokens``, ``temperature``, config sources, ...).

    Returns:
        The parsed JSON value (dict/list), or a validated ``model_cls``
        instance when given.

    Raises:
        ValueError: When every attempt fails to produce valid JSON.
    """
    if task_type is not None:
        solved = _try_deterministic(task_type, task_payload, prompt, model_cls)
        if solved is not _UNSOLVED:
            return solved

    if model_cls is not None:
        schema = model_cls.model_json_schema()

    suffix = _json_system_suffix(schema)
    full_system = f"{system}\n\n{suffix}" if system else suffix

    # Try native provider structured output first (skips the reprompt round-trip);
    # keep the schema-in-system suffix as belt-and-suspenders. If the provider
    # rejects response_format, drop it and fall back to the parse-retry path.
    native_rf = _json_response_format(schema)
    use_native = native_rf is not None

    attempt_prompt = prompt
    last_error = ""
    for _ in range(max(retries, 0) + 1):
        try:
            reply = await acomplete(
                attempt_prompt, system=full_system,
                response_format=native_rf if use_native else None, **kwargs,
            )
        except Exception as exc:  # provider likely rejected response_format
            if use_native:
                use_native = False
                last_error = str(exc)
                continue
            raise
        payload = _extract_json_text(reply)
        try:
            return _coerce_json(json.loads(payload), model_cls)
        except Exception as exc:  # json decode or pydantic validation
            last_error = str(exc)
            use_native = False  # native didn't help; reprompt in plain text mode
            attempt_prompt = (
                f"{prompt}\n\n"
                f"Your previous reply was not valid:\n{reply}\n\n"
                f"Error: {last_error}\n"
                "Reply again with ONLY corrected valid JSON."
            )
    raise ValueError(f"model did not produce valid JSON after {retries + 1} attempt(s): {last_error}")


def complete_json(
    prompt: str,
    *,
    schema: dict[str, Any] | None = None,
    model_cls: type | None = None,
    retries: int = 2,
    system: str | None = None,
    **kwargs: Any,
) -> Any:
    """Synchronous wrapper around :func:`acomplete_json`.

    Raises if called from within a running event loop — use
    :func:`acomplete_json` there instead.
    """
    _reject_running_loop("complete_json", "acomplete_json")
    return asyncio.run(
        acomplete_json(
            prompt,
            schema=schema,
            model_cls=model_cls,
            retries=retries,
            system=system,
            **kwargs,
        )
    )


def _reject_running_loop(sync_name: str, async_name: str) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(
        f"{sync_name}() cannot run inside an active event loop; await {async_name}()"
    )


def complete(
    prompt: str,
    *,
    system: str | None = None,
    images: list[str | bytes | Path] | None = None,
    config: Any | None = None,
    config_dict: dict[str, Any] | None = None,
    config_path: str | Path | None = None,
    model: str | None = None,
    preset: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    usage_sink: Any | None = None,
    response_format: dict[str, Any] | None = None,
    env: Any | None = None,
) -> str:
    """Synchronous wrapper around :func:`acomplete`.

    Raises if called from within a running event loop — use :func:`acomplete`
    there instead.
    """
    _reject_running_loop("complete", "acomplete")
    return asyncio.run(
        acomplete(
            prompt,
            system=system,
            images=images,
            config=config,
            config_dict=config_dict,
            config_path=config_path,
            model=model,
            preset=preset,
            max_tokens=max_tokens,
            temperature=temperature,
            usage_sink=usage_sink,
            response_format=response_format,
            env=env,
        )
    )
