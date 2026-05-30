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
import mimetypes
from pathlib import Path
from typing import Any


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
        images: Optional images (paths, http/data URLs, or raw bytes) for
            vision-capable models. Sent as OpenAI-style multimodal content.
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
    messages.append({"role": "user", "content": _user_content(prompt, images)})

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
    images: list[str | bytes | Path] | None = None,
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
            images=images,
            config_path=config_path,
            model=model,
            preset=preset,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    )
