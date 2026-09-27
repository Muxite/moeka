"""Chat message builders for ``LLM.generate`` (stdlib only).

Messages are plain OpenAI-shaped dicts; these helpers only save typing. Images
become OpenAI ``image_url`` content parts, which every provider translates.
"""

from __future__ import annotations

import base64
import mimetypes
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ImageInput = str | bytes | bytearray | Path


def image_part(image: ImageInput) -> dict[str, Any]:
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


def user_content(text: str, images: Iterable[ImageInput] | None = ()) -> Any:
    """Plain string when no images, else a multimodal content-part list."""
    parts = [image_part(image) for image in (images or ())]
    if not parts:
        return text
    return [{"type": "text", "text": text}, *parts]


def system(text: str) -> dict[str, Any]:
    return {"role": "system", "content": text}


def user(text: str, images: Iterable[ImageInput] = ()) -> dict[str, Any]:
    return {"role": "user", "content": user_content(text, images)}


def assistant(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": text}


__all__ = ["ImageInput", "assistant", "image_part", "system", "user", "user_content"]
