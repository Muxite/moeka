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
from pathlib import Path
from typing import Any


async def acomplete(
    prompt: str,
    *,
    system: str | None = None,
    config_path: str | Path | None = None,
    model: str | None = None,
    preset: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> str:
    """Run a single chat completion through moeka's provider layer.

    Args:
        prompt: The user message.
        system: Optional system prompt.
        config_path: Path to ``config.json``; defaults to ``~/.nanobot/config.json``.
        model: Override the resolved model (provider-specific id).
        preset: Named model preset to use instead of the active default.
        max_tokens / temperature: Generation overrides; ``None`` uses the
            provider's configured generation defaults.

    Returns:
        The assistant's text content.
    """
    from nanobot.config.loader import load_config, resolve_config_env_vars
    from nanobot.providers.factory import make_provider

    resolved = Path(config_path).expanduser().resolve() if config_path else None
    config = resolve_config_env_vars(load_config(resolved))
    provider = make_provider(config, preset_name=preset, model=model)

    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    response = await provider.chat_with_retry(
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    if response.content is None:
        raise RuntimeError(
            "moeka completion returned no content "
            f"(finish_reason={response.finish_reason!r}, "
            f"error_type={response.error_type!r})"
        )
    return response.content


def complete(
    prompt: str,
    *,
    system: str | None = None,
    config_path: str | Path | None = None,
    model: str | None = None,
    preset: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> str:
    """Synchronous wrapper around :func:`acomplete`.

    Raises if called from within a running event loop — use :func:`acomplete`
    there instead.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "complete() cannot run inside an active event loop; await acomplete()"
        )
    return asyncio.run(
        acomplete(
            prompt,
            system=system,
            config_path=config_path,
            model=model,
            preset=preset,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    )
