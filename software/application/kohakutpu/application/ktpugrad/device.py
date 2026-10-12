"""The tinygrad device: ``Tensor(x, device="KTPU")`` on a KohakuTPU (the
Verilated card, `runtime.device`).

The allocator's opaque is a `runtime.Storage`: a buffer's row-major bytes
plus the forms kernels made of it (`runtime.forms`); an upload records the
bytes it came from, a read back takes whichever form is current. The renderer
renders nothing: `seam.to_program` turned the ast into a plan, and the
program runs it.
"""

import sys
from types import ModuleType
from typing import Any

import numpy as np
from tinygrad.device import Allocator, Compiled
from tinygrad.renderer import Renderer

from kohakutpu.application.ktpugrad import run as R
from kohakutpu.application.ktpugrad.lower import Refused
from kohakutpu.application.ktpugrad.plans import PLANS, KTPUUnsupported
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime.rows import RowsError
from kohakutpu.application.runtime.storage import FLAT, Storage, StorageView

NAME = "KTPU"
MODULE = f"tinygrad.runtime.ops_{NAME.lower()}"
#: Device time a run is reported at: cycles over this clock (Hz).
CLOCK_HZ = 300e6


class KTPUAllocator(Allocator):
    """Storages on the runtime device."""

    @property
    def rt(self):
        return self.dev.rt

    def _alloc(self, size: int, options):
        return Storage(self.rt, size)

    def _free(self, opaque, options) -> None:
        if isinstance(opaque, Storage):
            opaque.free()

    def _copyin(self, dest, src: memoryview) -> None:
        raw = bytes(src)
        st, off = FM.base_of(dest)
        if off or len(raw) % 32:
            # A partial last word: keep the bytes past the buffer.
            lo, hi = off & ~31, -(-(off + len(raw)) // 32) * 32
            old = bytearray(self.rt.read(st.addr + lo, hi - lo))
            old[off - lo : off - lo + len(raw)] = raw
            self.rt.write(st.addr + lo, bytes(old))
        else:
            self.rt.write(st.addr, raw)
        if FLAT not in st.valid:
            st.wrote(FLAT)
        st.valid = {FLAT}
        st.host = raw if not off and len(raw) == st.nbytes else None
        st.dtype = None

    def _copyout(self, dest: memoryview, src) -> None:
        st, off = FM.base_of(src)
        n = len(dest)
        dt = getattr(st, "dtype", None)
        if dt is None or FLAT in st.valid:
            dest[:] = self.rt.read(st.addr + off, n)
            return
        dest[:] = FM.read(self.rt, st, dt, n // np.dtype(dt).itemsize)

    def _offset(self, buf, size: int, offset: int):
        if isinstance(buf, StorageView):
            return StorageView(buf.base, buf.offset + offset, size)
        return StorageView(buf, offset, size)


class KTPURenderer(Renderer):
    """The GPU assumptions this machine does not have: a unit is programmed,
    not commanded -- no threads, no shared memory, no warp tensor cores."""

    supports_float4 = False
    has_local = False
    has_shared = False
    has_threads = False
    shared_max = 0
    global_max = (1, 1, 1)
    local_max = (1, 1, 1)
    tensor_cores: list = []  # noqa: RUF012  (tinygrad reads it off the class)

    def render(self, uops) -> str:
        raise NotImplementedError(
            "the KTPU backend renders nothing: ktpugrad's to_program turns a kernel "
            "ast into a plan (is the seam installed? import kohakutpu.application.ktpugrad)"
        )


class KTPUProgram:
    """A planned kernel, called where a compiled program would be."""

    def __init__(self, dev, obj) -> None:
        self.dev = dev
        plan = PLANS.get(bytes(obj.lib))
        if plan is None:
            raise RuntimeError(
                f"no KTPU plan for {obj.name!r}: it did not come through the seam"
            )
        self.plan = plan
        self.slots = [s for _, s, _, _ in obj.signature]

    def __call__(
        self, *bufs, global_size=None, local_size=None, vals=(), wait=False, **kw
    ):
        by_slot = dict(zip(sorted(self.slots), bufs))
        try:
            runs = R.execute(self.dev.rt, self.plan, by_slot)
        except (Refused, RowsError, FM.FormError) as e:
            # Refused once the buffers are known (their forms, a sub-buffer, ...).
            raise KTPUUnsupported(
                f"KTPU refuses kernel {self.plan.name}: {e}"
            ) from None
        self.dev.last_runs = runs
        return sum(r.cycles for r in runs) / CLOCK_HZ


class KTPUDevice(Compiled):
    """A KohakuTPU, as tinygrad sees it: the runtime's device (the simulator
    unless told otherwise)."""

    def __init__(self, device: str = NAME) -> None:
        self.rt = D.device()
        self.last_runs: list = []
        # KTPUProgram is a runtime by duck type (called as tinygrad calls one).
        runtime: Any = KTPUProgram
        super().__init__(device, KTPUAllocator(self), [KTPURenderer], runtime)

    def synchronize(self) -> None:
        """Runs are synchronous: nothing to wait for."""


def register() -> ModuleType:
    """Make ``Device["KTPU"]`` resolve without writing into tinygrad's tree:
    `_Device` imports ``tinygrad.runtime.ops_ktpu`` and takes the class
    named ``KTPUDevice``; a module already in `sys.modules` is used as is."""
    mod = sys.modules.get(MODULE)
    if mod is None:
        mod = ModuleType(MODULE)
        mod.__doc__ = "Synthesised by kohakutpu.application.ktpugrad."
        sys.modules[MODULE] = mod
    vars(mod)["KTPUDevice"] = KTPUDevice
    return mod


__all__ = [
    "CLOCK_HZ",
    "MODULE",
    "NAME",
    "KTPUAllocator",
    "KTPUDevice",
    "KTPUProgram",
    "KTPURenderer",
    "register",
]
