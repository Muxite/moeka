"""moeka: the stable public API of the moeka kernel.

This package only re-exports; the implementation lives in ``nanobot.kernel``.
Build an :class:`Environment` with :meth:`Environment.for_host`, then open a
:class:`Kernel` on it::

    from moeka import Environment, Kernel, ModelSpec, ProviderSpec

    env = Environment.for_host(
        state_dir=state, work_dir=work, credentials={"openai": key},
        providers=[ProviderSpec(name="openai", credential="openai")],
        models=[ModelSpec(name="main", model="gpt-4.1", provider="openai")],
        default_model="main",
    )
    with Kernel(env) as kernel:
        ...
"""

from nanobot.kernel.env import CredentialResolver, Paths, StaticCredentialResolver
from nanobot.kernel.hostenv import Environment, ModelSpec, ProviderSpec
from nanobot.kernel.kernel import Kernel
from nanobot.kernel.sampling import Sampling

__all__ = [
    "CredentialResolver",
    "Environment",
    "Kernel",
    "ModelSpec",
    "Paths",
    "ProviderSpec",
    "Sampling",
    "StaticCredentialResolver",
]
