"""The memory mover for values: its command registers (control-registers.md
s3) as writes, each GO run to its end on a memory.

A walker steps 32-byte words: element i (innermost level fastest) at ``base +
sum idx * stride``, and each bound axis at ``abase + sum idx * astep`` over the
levels bound to it; an element is inside when every axis with an extent has
``0 <= axis < extent`` (`mx_tdesc.v`). A source element outside reads the
immediate (its low 8 / 16 / 32 bits repeated across the word); a destination
element outside is not written. The destination's walk orders a copy, a fill
and a gather; the source's a scatter and a transform.

`memory` has ``read_block(addr, n) -> bytes`` and ``write_block(addr, data)``.
"""

from dataclasses import dataclass, field

import numpy as np

from kohakuaccel.compiler.package import mover as PM
from kohakutpu.compiler.layout import tensor

WORD = 32


class MoverFault(RuntimeError):
    """A move the mover faults on (its fault code, control-registers.md s3)."""

    def __init__(self, code: int, why: str) -> None:
        super().__init__(f"mover fault {code}: {why}")
        self.code = code


@dataclass
class _Walker:
    base: int = 0
    ndim: int = 0
    dims: dict = field(default_factory=dict)  # level -> (count, stride, axis, astep)
    windows: dict = field(default_factory=lambda: {1: (0, 0), 2: (0, 0)})
    xform: tuple = (0, 0)

    def elements(self) -> tuple:
        """``(addresses, inside)`` of every element in walk order."""
        levels = [self.dims.get(d, (1, 0, 0, 0)) for d in range(self.ndim)]
        addr = np.array([self.base], np.int64)
        axes = {1: np.array([self.windows[1][0]]), 2: np.array([self.windows[2][0]])}
        for count, stride, axis, astep in levels:
            idx = np.arange(count, dtype=np.int64)
            addr = (addr[:, None] + idx * stride).reshape(-1)
            for a in (1, 2):
                step = astep if axis == a else 0
                axes[a] = (axes[a][:, None] + idx * step).reshape(-1)
        inside = np.ones(len(addr), bool)
        for a in (1, 2):
            ext = self.windows[a][1]
            if ext:
                inside &= (axes[a] >= 0) & (axes[a] < ext)
        return addr, inside


class Mover:
    """The registers and the moves they start."""

    def __init__(self, memory) -> None:
        self.read, self.put = memory.read_block, memory.write_block
        self.walkers = [_Walker(), _Walker()]
        self.mode = self.ew = self.flags = 0
        self.fp = False
        self.index = (0, 0)
        self.seed = self.imm = 0
        self.row = (0, 0)
        self.staged: tuple | None = None  # (walker, level, count, stride)
        #: moves finished (MVSTAT's done count)
        self.moves = 0

    def write(self, reg: int, value: int) -> None:
        if reg == PM.R_CTRL:
            self.mode, self.ew = value & 7, (value >> 3) & 3
            self.flags, self.fp = (value >> 8) & 0xFF, bool(value & PM.INDEX_FP)
            if value & PM.GO:
                self._go()
        elif reg == PM.R_HDR:
            w = self.walkers[value & 1]
            w.base = (value >> PM.BASE_LSB) & ((1 << PM.ADDR_BITS) - 1)
            w.ndim = (value >> 44) & 7
            w.xform = ((value >> PM.XID_LSB) & 0xF, (value >> PM.XMODE_LSB) & 0xF)
        elif reg == PM.R_DIM:
            stride = _signed((value >> 20) & 0xFFFFFFFF, 32)
            self.staged = (value & 1, (value >> 1) & 7, (value >> 4) & 0xFFFF, stride)
        elif reg == PM.R_AXIS:
            if self.staged is not None:
                sel, level, count, stride = self.staged
                axis, astep = value & 3, _signed((value >> 2) & 0xFFFF, 16)
                self.walkers[sel].dims[level] = (count, stride, axis, astep)
        elif reg == PM.R_WINDOW:
            w = self.walkers[value & 1]
            axis = 1 + ((value >> 1) & 1)
            w.windows[axis] = (
                _signed((value >> 2) & 0xFFFF, 16),
                (value >> 18) & 0xFFFF,
            )
        elif reg == PM.R_INDEX:
            self.index = (value & ((1 << PM.ADDR_BITS) - 1), (value >> 40) & 0xFFFF)
        elif reg == PM.R_SEED:
            self.seed = value
        elif reg == PM.R_IMM:
            self.imm = value & 0xFFFFFFFF
        elif reg == PM.R_ROW:
            self.row = (value & 0xFFFFFFFF, (value >> 32) & 0xFFFF)

    # ---------------------------------------------------------------- moves
    def _go(self) -> None:
        if self.ew == 3:
            raise MoverFault(5, "element width 3")
        mode = PM.COPY if self.mode == 7 else self.mode
        run = {
            PM.COPY: self._copy,
            PM.FILL: self._fill,
            PM.GATHER: self._gather,
            PM.SCATTER: self._scatter,
            PM.XFORM: self._xform,
        }.get(mode)
        if run is None:
            raise MoverFault(4, f"mode {self.mode} has no model")
        run()
        self.moves += 1

    def _pad(self) -> bytes:
        width = (1, 2, 4)[self.ew]
        unit = (self.imm & ((1 << (8 * width)) - 1)).to_bytes(width, "little")
        return unit * (WORD // width)

    def _copy(self) -> None:
        saddr, sin = self.walkers[0].elements()
        daddr, din = self.walkers[1].elements()
        pad = self._pad()
        for i in range(len(daddr)):
            if din[i]:
                data = self.read(int(saddr[i]), WORD) if sin[i] else pad
                self.put(int(daddr[i]), data)

    def _fill(self) -> None:
        daddr, din = self.walkers[1].elements()
        pad = self._pad()
        for a in daddr[din]:
            self.put(int(a), pad)

    def _indices(self) -> np.ndarray:
        base, count = self.index
        if count > PM.MAX_INDEX:
            raise MoverFault(1, f"{count} indices")
        raw = self.read(base, 4 * count)
        if not self.fp:
            return np.frombuffer(raw, np.uint32).astype(np.int64)
        v = np.frombuffer(raw, np.float32).astype(np.float64)
        if not np.all((v == np.floor(v)) & (v >= 0) & (v < 2**31)):
            raise MoverFault(10, "an fp32 index that is not an index")
        return v.astype(np.int64)

    def _gather(self) -> None:
        idx, (pitch, words) = self._indices(), self.row
        daddr, din = self.walkers[1].elements()
        base = self.walkers[0].base
        for i, a in enumerate(daddr):
            r, j = divmod(i, words)
            if din[i]:
                self.put(int(a), self.read(base + int(idx[r]) * pitch + WORD * j, WORD))

    def _scatter(self) -> None:
        idx, (pitch, words) = self._indices(), self.row
        base = self.walkers[1].base
        if base % WORD or pitch % WORD:
            raise MoverFault(6, "a scatter's base or pitch off a word")
        saddr, sin = self.walkers[0].elements()
        pad = self._pad()
        for i, a in enumerate(saddr):
            r, j = divmod(i, words)
            data = self.read(int(a), WORD) if sin[i] else pad
            self.put(base + int(idx[r]) * pitch + WORD * j, data)

    def _xform(self) -> None:
        """Entries of source words (the source walker counts words) through
        the transform bank's occupant; the destination walker counts entries,
        each entry's words written from its element on."""
        xid, xmode = self.walkers[0].xform
        saddr, sin = self.walkers[0].elements()
        if not sin.all():
            raise MoverFault(7, "a bound axis in a transform move")
        per, out = {0: (4, 4), 1: (8, 4), 2: (4, 4)}.get(xid, (0, 0))
        if not per:
            raise MoverFault(8, f"transform id {xid} names no occupant")
        daddr, din = self.walkers[1].elements()
        for e in range(min(len(saddr) // per, len(daddr))):
            src = saddr[e * per : (e + 1) * per]
            got = _occupant(xid, xmode, b"".join(self.read(int(a), WORD) for a in src))
            if din[e]:
                self.put(int(daddr[e]), got[: out * WORD])


def _occupant(xid: int, xmode: int, words: bytes) -> bytes:
    """One entry through occupant `xid` (`xform_bank.v`)."""
    if xid == 0:
        return words
    if xid == 1:
        # The quantiser: 4 lanes x 32 fp16 to one MXFP7 entry, A or B packing.
        x = np.frombuffer(words, np.float16).reshape(4, 32)
        return tensor.pack(x, 1, 1, xmode & 1)
    # GT4: out word i, granule w = in word w, granule i (64-bit granules).
    g = np.frombuffer(words, np.uint64).reshape(4, 4)
    return np.ascontiguousarray(g.T).tobytes()


def _signed(v: int, bits: int) -> int:
    return v - (1 << bits) if v >> (bits - 1) else v
