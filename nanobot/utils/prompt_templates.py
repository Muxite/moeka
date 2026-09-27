"""Load and render agent system prompt templates (Jinja2) under nanobot/templates/.

Agent prompts live in ``templates/agent/`` (pass names like ``agent/identity.md``).
Shared copy lives under ``agent/_snippets/`` and is included via
``{% include 'agent/_snippets/....md' %}``.

``roots`` (a variant's ``templates_dir``, Task 8) are searched before the built-in
tree: a file there shadows the built-in template of the same relative name, includes
too, and a name missing from every root falls back to the built-in file. One Jinja
``Environment`` is cached per roots tuple, so the base case (``roots=()``) is the
same single cached environment as before.
"""

from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

_TEMPLATES_ROOT = Path(__file__).resolve().parent.parent / "templates"


@lru_cache(maxsize=64)
def _environment(roots: tuple[str, ...] = ()) -> Environment:
    # Plain-text prompts: do not HTML-escape variable values.
    return Environment(
        loader=FileSystemLoader([*roots, str(_TEMPLATES_ROOT)]),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_template(
    name: str, *, strip: bool = False, roots: Sequence[Path | str] = (), **kwargs: Any,
) -> str:
    """Render ``name`` (e.g. ``agent/identity.md``, ``agent/platform_policy.md``) under ``templates/``.

    Use ``strip=True`` for single-line user-facing strings when the file ends
    with a trailing newline you do not want preserved. ``roots`` are searched, in
    order, before the built-in templates (see the module docstring).
    """
    env = _environment(tuple(str(root) for root in roots))
    text = env.get_template(name).render(**kwargs)
    return text.rstrip() if strip else text
