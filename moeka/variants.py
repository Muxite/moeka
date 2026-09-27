"""Variants: per-kernel overrides of what the model sees, and its fingerprint.

``Kernel(env, variant=Variant(...))`` loads skills, prompt templates, bootstrap text and
tool descriptions for that kernel's agents only; ``Fingerprint`` digests the rendered
system prompt, tool definitions, model and sampling (see ``nanobot.kernel.variants``).
"""

from nanobot.kernel.variants import Fingerprint, Variant

__all__ = ["Fingerprint", "Variant"]
