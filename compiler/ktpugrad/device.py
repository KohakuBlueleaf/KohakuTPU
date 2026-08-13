"""A tinygrad device whose memory is a KohakuTPU arena.

The four objects `tinygrad.device.Compiled` wants, three of which are nearly
empty here: this is a library-dispatch backend, so the renderer renders nothing
and the compiler compiles nothing. What is real is the allocator, which is
`kohakuaccel.rt.Runtime`'s four memory methods under tinygrad's names.
"""

import functools
import sys
from types import ModuleType
from typing import ClassVar

from tinygrad.device import Allocator, Compiled
from tinygrad.renderer import Renderer

#: What `Tensor.to(...)` is spelled with, and the tail of the module name
#: `tinygrad.device._Device` will try to import to find us.
NAME = "KTPU"
MODULE = f"tinygrad.runtime.ops_{NAME.lower()}"

#: Every transport under `Runtime.write` takes whole 64-bit words, so a copy of
#: an odd number of bytes is rounded up into the span the arena already aligned.
WORD_BYTES = 8


def _words(nbytes: int) -> int:
    """`nbytes` rounded up to a whole transport word."""
    return -(-nbytes // WORD_BYTES) * WORD_BYTES


class KTPUAllocator(Allocator):
    """Arena spans on one attached machine, addressed as flat bytes.

    The opaque a buffer holds is its arena address. Nothing here knows a byte
    order: a tinygrad buffer is row-major fp16 and a kernel operand is in a
    device layout, and turning one into the other is the runner's job.
    """

    @property
    def rt(self):
        """The KohakuTPU runtime this arena lives on."""
        return self.dev.rt

    def _alloc(self, size: int, options):
        """Reserve `size` bytes. Raises :class:`OutOfMemory` when none fit."""
        return self.rt.alloc(size)

    def _free(self, opaque, options) -> None:
        """Return a span. Raises :class:`KeyError` if it was never allocated."""
        self.rt.free(opaque)

    def _copyin(self, dest: int, src: memoryview) -> None:
        """Upload `src` to address `dest`, padded to a whole transport word."""
        blob = bytes(src)
        self.rt.write(dest, blob.ljust(_words(len(blob)), b"\0"))

    def _copyout(self, dest: memoryview, src: int) -> None:
        """Read back into `dest` from address `src`."""
        dest[:] = self.rt.read(src, _words(len(dest)))[: len(dest)]

    def _offset(self, buf: int, size: int, offset: int) -> int:
        """A sub-span of an existing one. An arena address is just a number."""
        return buf + offset


class KTPURenderer(Renderer):
    """The GPU assumptions this machine does not have.

    A unit is programmed, not commanded: there is no thread, no program id and
    no barrier inside a kernel, so there is nothing for local or shared memory
    to mean. Tensor cores are a warp cooperating on a fragment and our GEMM is a
    whole-tile instruction, and the vector width is a codegen concern this
    backend does not reach.
    """

    supports_float4 = False
    has_local = False
    has_shared = False
    has_threads = False
    shared_max = 0
    global_max = (1, 1, 1)
    local_max = (1, 1, 1)
    tensor_cores: ClassVar[list] = []

    def render(self, uops) -> str:
        """Refuse: this backend dispatches a library kernel, it does not codegen.

        Raises :class:`NotImplementedError` always. Reaching here means the
        matcher refused an ast and tinygrad's own lowering was asked for it.
        """
        raise NotImplementedError(
            "the KTPU backend renders nothing; it matches a scheduled kernel "
            "against kohakutpu.kernels and dispatches that"
        )


class KTPUDevice(Compiled):
    """An attached KohakuTPU, as tinygrad sees it.

    The machine comes from `kohakutpu.api.device`, which is the project's one
    shared helper and defaults to the simulator: reaching hardware stays a
    decision made somewhere else.
    """

    def __init__(self, device: str = NAME) -> None:
        # Imported here, not at module scope: registering the device must not
        # cost an import of the kernel library or the unit models.
        from kohakutpu import api
        from ktpugrad.runner import KTPUProgram

        #: The KohakuTPU runtime this device's buffers and kernels live on.
        self.rt = api.device()
        super().__init__(
            device,
            KTPUAllocator(self),
            [KTPURenderer],
            functools.partial(KTPUProgram, self),
        )

    def synchronize(self) -> None:
        """Wait for outstanding work on the machine."""
        self.rt.sync()


def register() -> ModuleType:
    """Make `Device["KTPU"]` resolve, without writing into tinygrad's tree.

    `_Device` imports `tinygrad.runtime.ops_ktpu` and takes the class named
    `KTPUDevice` out of it; a module already in `sys.modules` is returned as it
    stands, so a synthetic one is enough. Returns that module.
    """
    mod = sys.modules.get(MODULE)
    if mod is None:
        mod = ModuleType(MODULE)
        mod.__doc__ = "Synthesised by ktpugrad.device.register."
        sys.modules[MODULE] = mod
    mod.KTPUDevice = KTPUDevice
    return mod
