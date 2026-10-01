"""Variants: per-kernel overrides of what the model sees, and its fingerprint.

``Kernel(env, variant=Variant(...))`` loads skills, prompt templates, bootstrap text,
tool descriptions and tool parameter descriptions for that kernel's agents only;
``Fingerprint`` digests the rendered system prompt, tool definitions, model, sampling
and every byte of the effective skill set (see ``nanobot.kernel.variants``).
``VariantError`` (also in ``moeka.errors``) is a parameter path that does not resolve.
"""

from nanobot.kernel.variants import Fingerprint, Variant, VariantError

__all__ = ["Fingerprint", "Variant", "VariantError"]
