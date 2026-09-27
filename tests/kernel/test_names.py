"""The kernel package exposes the same surface as nanobot.core under kernel names."""

import nanobot.core
import nanobot.kernel


def test_moeka_kernel_is_moeka_core():
    assert nanobot.kernel.MoekaKernel is nanobot.core.MoekaCore
    assert nanobot.core.MoekaKernel is nanobot.core.MoekaCore


def test_kernel_reexports_core_surface():
    for name in nanobot.core.__all__:
        assert getattr(nanobot.kernel, name) is getattr(nanobot.core, name)
    assert "MoekaKernel" in nanobot.kernel.__all__
