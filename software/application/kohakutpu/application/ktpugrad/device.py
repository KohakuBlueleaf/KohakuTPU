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
from typing import Any, cast

import numpy as np
from tinygrad.device import Allocator, Buffer, Compiled, Device
from tinygrad.engine.jit import GraphRunner
from tinygrad.renderer import Renderer

from kohakutpu.application.ktpugrad import fold as F
from kohakutpu.application.ktpugrad import run as R
from kohakutpu.application.ktpugrad.lower import Refused
from kohakutpu.application.ktpugrad.plans import PLANS, KTPUUnsupported
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime.rows import RowsError
from kohakutpu.application.runtime.storage import FLAT, Storage, StorageView
from kohakutpu.compiler import verify as V

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


class KTPUGraph(GraphRunner):
    """A TinyJit batch of KTPU kernels, replayed as the card ran it.

    Each call first copies the batch's inputs into slots of its own (fixed
    addresses) from their host bytes, with every host-made f16 form the
    batch read last call (an int label's), then runs the kernels. While the
    batch is not yet known to be periodic, the run goes through the planner
    with the card's log on (`NodeCard.record`). Two calls in a row that log
    the same events -- packages, bindings, host writes, no host read -- make
    that log the batch, and later calls replay it: every package already in
    its node's heap, submitted back to back, the host waiting once.

    Inputs without host bytes (made on the card) run through the planner
    every call."""

    def __init__(self, linear, input_uops=()) -> None:
        super().__init__(linear, input_uops)
        self.dev = cast("KTPUDevice", Device[NAME])
        self.slots: dict = {}
        #: per slot, the element type of the f16 form the batch read from host
        self.host_forms: dict = {}
        #: the last recorded call: (entry state, log, exit state, exit records)
        self.prev: tuple | None = None
        #: the verified call, replayed from an equal entry state
        self.log: tuple | None = None
        self.counts = dict.fromkeys(("planned", "recorded", "replayed"), 0)
        #: why each pair of consecutive records differed
        self.misses: list = []

    def _fill(self, input_uops) -> bool:
        """Every input's host bytes into its slot; False where one has none."""
        used = {i for rep in self.uop_replace for _, i in rep}
        for i in used:
            buf = input_uops[i].buffer
            st, off = FM.base_of(buf._buf)
            if off or not isinstance(st, Storage) or st.host is None:
                return False
            if len(st.host) != buf.nbytes:
                return False
            slot = self.slots.get(i)
            if slot is None:
                slot = Buffer(buf.device, buf.size, buf.dtype).ensure_allocated()
                self.slots[i] = slot
            self.dev.allocator._copyin(slot._buf, memoryview(st.host))
            dt = self.host_forms.get(i)
            if dt is not None:
                FM.f16(self.dev.rt, slot._buf, dt, buf.size)
        return True

    def _plan(self, input_uops, slots: bool) -> None:
        for j, ((_, _, bufs, _), runtime) in enumerate(
            zip(self.calls, self.runtimes, strict=True)
        ):
            args = [b._buf for b in bufs]
            for pos, i in self.uop_replace[j]:
                src = self.slots[i] if slots else input_uops[i].buffer
                args[pos] = src._buf
            assert runtime is not None
            runtime(*args)

    def _storages(self) -> list:
        """Every storage the batch's kernels name (its slots included)."""
        seen: dict = {}
        for _, _, bufs, _ in self.calls:
            for b in bufs:
                st, _ = FM.base_of(b._buf)
                seen.setdefault(id(st), st)
        for s in self.slots.values():
            seen.setdefault(id(s._buf), s._buf)
        return list(seen.values())

    @staticmethod
    def _cores(cores: dict) -> tuple:
        """What every node's vector cores hold (a package is built for it)."""
        return tuple((n, V.cores_digest(c)) for n, c in sorted(cores.items()))

    @staticmethod
    def _state(storages: list) -> tuple:
        """What the planner decides from: each storage's valid forms, whether
        it holds host bytes, its element type, its forms' places."""
        return tuple(
            (
                frozenset(st.valid),
                st.host is not None,
                st.dtype,
                tuple((k, f.addr, f.nbytes) for k, f in st.forms.items()),
            )
            for st in storages
        )

    def __call__(self, input_uops, var_vals, wait=False):
        rt = self.dev.rt
        if not self._fill(input_uops):
            self.counts["planned"] += 1
            self.prev = self.log = None
            self._plan(input_uops, slots=False)
            return None
        storages = self._storages()
        cores = rt.card.residents
        entry = (self._state(storages), self._cores(cores), rt.private_state())
        if self.log is not None and entry == self.log[0]:
            self.counts["replayed"] += 1
            runs = rt.replay(self.log[1])
            # The planner's bookkeeping as the recorded call left it: a
            # replay changes the card's bytes, not the storages' records or
            # what the vector cores are known to hold.
            objs, held = self.log[3]
            for st, (valid, host, dtype, forms) in zip(storages, objs, strict=True):
                st.valid, st.dtype, st.forms = set(valid), dtype, dict(forms)
                if not host:
                    st.host = None
            for n, snap in held.items():
                V.cores_restore(cores[n], snap)
            return sum(r.cycles for r in runs) / CLOCK_HZ
        self.counts["recorded"] += 1
        rt.record()
        try:
            self._plan(input_uops, slots=True)
        finally:
            log = rt.recorded()
        for i, slot in self.slots.items():
            st = slot._buf
            dt = F.np_dtype(slot.dtype)
            if np.dtype(dt) != np.float16 and FM.F16 in st.valid:
                self.host_forms[i] = dt
        exit_ = (self._state(storages), self._cores(cores), rt.private_state())
        objs = [
            (set(st.valid), st.host is not None, st.dtype, dict(st.forms))
            for st in storages
        ]
        held = {n: V.cores_snapshot(c) for n, c in cores.items()}
        got = (entry, log, exit_, (objs, held))
        reads = any(ev[0] == "read" for ev in log)
        if not reads and self.prev is not None and got[:3] == self.prev[:3]:
            self.log = got
        elif self.prev is not None:
            self.misses.append(_first_difference(self.prev[:3], got[:3]))
        self.prev = None if reads else got
        return None


def _first_difference(a: tuple, b: tuple) -> str:
    """Where two records (entry state, log, exit state) first differ."""
    for name, x, y in zip(("entry", "log", "exit"), a, b, strict=True):
        if x == y:
            continue
        if len(x) != len(y):
            return f"{name}: {len(x)} vs {len(y)} items"
        k = next(i for i, (p, q) in enumerate(zip(x, y, strict=True)) if p != q)
        p, q = x[k], y[k]
        if name == "log" and p[0] == q[0] == "run":
            for n in p[1]:
                (pa, pb), (qa, qb) = p[1][n], q[1].get(n, (b"", ()))
                if pb != qb:
                    return f"log run {k} node {n}: bindings {pb} vs {qb}"
                at = next((i for i, (u, v) in enumerate(zip(pa, qa)) if u != v), None)
                return (
                    f"log run {k} node {n}: package {len(pa)} vs {len(qa)} B, "
                    f"first differs at byte {at}"
                )
        return f"{name} item {k}: {str(p)[:200]} vs {str(q)[:200]}"
    return "equal"


class KTPUDevice(Compiled):
    """A KohakuTPU, as tinygrad sees it: the runtime's device (the simulator
    unless told otherwise)."""

    def __init__(self, device: str = NAME) -> None:
        self.rt = D.device()
        self.last_runs: list = []
        # KTPUProgram is a runtime by duck type (called as tinygrad calls one).
        runtime: Any = KTPUProgram
        super().__init__(
            device, KTPUAllocator(self), [KTPURenderer], runtime, graph=KTPUGraph
        )

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
    "KTPUGraph",
    "KTPUProgram",
    "KTPURenderer",
    "register",
]
