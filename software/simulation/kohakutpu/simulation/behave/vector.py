"""A V2 vector core for values (`v2_core.v`, vector-core-v2.md, vector-core.md
s3-4): the CU's IMEM / DESC / RUN words, a RUN's image run in program order.

Program order is the meaning: the engines overlap only where the syncs allow,
and the static checker proves a compiled program race-free, so syncs, marks,
waits and VBAR do nothing here. A register is 8 chunks of 16 lanes of E8M15
values (float64). A math op is the core's FMA, a·b + c rounded once (`max`,
`min`, `sel` and compares pick an operand); a transcendental is the exact
function rounded to E8M15, a few ulp from the card's 32-segment tables. S / K
registers hold 24-bit E8M15 words, or a count. VWAIT blocks the RUN until its
counter (granules landed by COUNT bursts, credits) reaches its target."""

import struct
from collections import deque

import numpy as np

from kohakutpu.compiler.encode import vector as A
from kohakutpu.compiler.isa import core as C
from kohakutpu.compiler.isa.vector import ISA as VEC
from kohakutpu.compiler.layout import tensor
from kohakutpu.language.numerics.e8m15 import value as e8

L1_WORDS = 512
WORD = 32
CHUNKS, LANES = 8, 16
#: EXP2D's sticky threshold (`v2_core` TAU): a result at or past 2^TAU.
TAU = 8
#: CU_SIGNAL INST_COMPLETE: each CU word retires with one.
COMPLETE = 0x00
_BY_CODE = {code: op for op, code in C.OPS.items()}


class Unmodelled(NotImplementedError):
    """An instruction the vector model does not hold yet, named."""


def from_bits(bits: int) -> float:
    """A 24-bit E8M15 word's value."""
    return struct.unpack("<f", struct.pack("<I", (bits & 0xFFFFFF) << 8))[0]


def to_bits(x: float) -> int:
    """`x` rounded to E8M15, as its 24-bit word."""
    v = float(e8(x))
    return struct.unpack("<I", struct.pack("<f", v))[0] >> 8


ONE, MINUS_ONE = to_bits(1.0), to_bits(-1.0)


class VectorCore:
    """One core at `at` (mesh, x, y) on card `net` (`card.Card`: credits and
    peer bursts go through it): `memory` its DRAM (``read_block`` /
    ``write_block``), `regs` its vector registers. A process: CU words queue
    (`deliver`), `step` runs as far as the counters let it."""

    def __init__(self, net, at: tuple, memory, regs: int = A.REGS) -> None:
        self.net, self.at, self.mem = net, at, memory
        self.words: deque = deque()
        self.task = None  # a RUN, until its VHALT
        self.out: list = []  # signals raised: (code, arg)
        self.done = 0  # instructions run (progress)
        #: RX granules landed by COUNT bursts, CR credits; what waits asked for
        self.count = {C.RX: 0, C.CR: 0}
        self.target = {C.RX: 0, C.CR: 0}
        self.xr = max(0, (regs - 1).bit_length() - 5)
        self.imem: dict = {}
        self.desc: dict = {}
        self.l1 = bytearray(L1_WORDS * WORD)
        self.v = np.zeros((regs, CHUNKS, LANES))
        self.p = np.zeros((4, CHUNKS, LANES), bool)
        self.s = [0] * 16
        self.k = [0, ONE, MINUS_ONE, 0]
        self.sticky = False
        # Walk configuration and increments, kept across RUNs as v2_core does.
        self.ptr = {"UPTR": 0, "UINC": 0, "PPTR": 0, "PINC": 0}
        self.stride = {"USTR": (1, 4), "PSTR": (1, 4)}
        self.ainc: dict = {}
        self.vl = A.VLMAX
        self.chk = ((0, 1),) * 4  # a, b, c, d: (base chunk, stride)
        self.xb = (A.XM_NONE, 0, 0, 0, 0, 0)  # xm, xk, sh, xc, pm, pr

    # ---------------------------------------------------------------- CU
    def deliver(self, payload: int) -> None:
        self.words.append(payload)

    def step(self) -> bool:
        """Run what can run: the RUN in progress, then the next CU words (each
        retires with a COMPLETE, a RUN once it halted). Whether anything ran."""
        before = self.done
        while True:
            if self.task is not None:
                try:
                    next(self.task)
                    return self.done != before  # blocked on a counter
                except StopIteration:
                    self.task = None
                    self.out.append((COMPLETE, 0))
                    self.done += 1
            if not self.words:
                return self.done != before
            payload = self.words.popleft()
            self.done += 1
            fmt = VEC.set.find(payload)
            if fmt is None:
                raise Unmodelled(
                    f"a word that decodes as no vector CU word: {payload:#x}"
                )
            f = fmt.decode(payload)
            if fmt.name == "IMEM":
                self.imem[VEC.imem_addr(f)] = VEC.imem_word(payload)
            elif fmt.name == "DESC":
                hi = f["value_hi"] << VEC.cfg.desc_value_bits
                self.desc[(f["ad"], f["fld"])] = f["value"] | hi
            else:
                self.task = self._run(f["pc"])
                continue
            self.out.append((COMPLETE, 0))

    def land(self, buf: int, off: int, data: bytes, count: bool) -> None:
        """A CU_DATA burst: granules from L1 word `off` (buffer 0 only)."""
        if buf != 0 or off + len(data) // WORD > L1_WORDS:
            raise Unmodelled(f"a burst into buffer {buf} at {off} (F_CUDATA)")
        self.l1[off * WORD : off * WORD + len(data)] = data
        if count:
            self.count[C.RX] += len(data) // WORD

    def credit(self, n: int) -> None:
        self.count[C.CR] += n

    # ------------------------------------------------------------- front
    def _decode(self, word: int) -> tuple:
        low = word & 0xFFFFFFFF
        fmt = C.CORE.find(low)
        if fmt is None:
            raise Unmodelled(f"core word {word:#x} decodes as no instruction")
        f = dict(fmt.decode(low))
        hi, mask = word >> 32, (1 << self.xr) - 1
        for k, name in enumerate(("vc", "vb", "va", "vd")):
            part = (hi >> (k * self.xr)) & mask if self.xr else 0
            if name in f:
                f[name] |= part << 5
            if name == "vd" and "reg" in f:
                f["reg"] |= part << 5
        return fmt.name, f

    def _run(self, pc: int):
        """The image from `pc` to its VHALT; yields while a VWAIT's counter is
        short of its target."""
        roff: dict = {}  # descriptor -> running offset, from 0 each RUN
        loop = None  # (first pc, end pc, trips left)
        for _ in range(50_000_000):
            self.done += 1
            word = self.imem.get(pc)
            if word is None:
                raise Unmodelled(f"pc {pc} holds no IMEM word")
            name, f = self._decode(word)
            step = 1
            if name == "VHALT":
                return
            if name == "VWAIT":
                c = f["counter"]
                self.target[c] += f["n"]
                while self.count[c] < self.target[c]:
                    yield
            elif name == "VPOST":
                self.net.credit(self.at[0], (f["dst_x"], f["dst_y"]), f["n"])
            elif name == "VSETI":
                imm = self.imem.get(pc + 1, 0) & 0xFFFFFF
                if f["to_k"]:
                    self.k[3] = imm
                else:
                    self.s[f["sreg"]] = imm
                step = 2
            elif name == "VLOOP":
                trips = self.s[f["sreg"]]
                if trips == 0:
                    step = 1 + f["body"]
                else:
                    loop = (pc + 1, pc + 1 + f["body"], trips)
            elif name == "VSKIPZ":
                if self.s[f["sreg"]] == 0:
                    step = 1 + f["skip"]
            elif name == "VSETVL":
                self.vl = self.s[f["sreg"]]
                if not 1 <= self.vl <= A.VLMAX:
                    raise Unmodelled(f"pc {pc}: VSETVL to {self.vl} (F_VL)")
            elif name.lower() in C.OPS:
                self._math(name.lower(), f)
            elif name in ("VUNPK", "VPACK"):
                self._walk(name, f)
            elif name in ("VFILL", "VDRAIN"):
                at = roff.get(f["ad"], 0) if f["rel"] else 0
                if f["rel"]:
                    roff[f["ad"]] = at + self.ainc.get(f["ad"], 0)
                self._dma(name, f, at)
            else:
                self._config(name, f)
            pc += step
            if loop is not None and pc >= loop[1]:
                first, end, left = loop
                if left > 1:
                    loop, pc = (first, end, left - 1), first
                else:
                    loop = None
        raise Unmodelled(f"pc {pc}: 50M instructions and no VHALT")

    def _config(self, name: str, f: dict) -> None:
        if name == "CHK":
            self.chk = self._nibs(f)
        elif name == "CHKVL":
            self.chk, self.vl = self._nibs(f), f["vl1"] + 1
        elif name == "XB":
            self.xb = (f["xm"], f["xk"], f["sh"], f["xc"], f["pm"], f["pr"])
        elif name in self.ptr:
            self.ptr[name] = f["ptr"] % L1_WORDS
        elif name in self.stride:
            self.stride[name] = (_signed(f["s0"], 9), _signed(f["s1"], 9))
        elif name == "GETSTK":
            self.s[f["sreg"]] = ONE if self.sticky else 0
            self.sticky = False
        elif name == "AINC":
            self.ainc[f["ad"]] = f["inc"]
        elif name in ("VSYNC", "VMARK", "VBAR"):
            pass  # ordering only: program order already holds it
        else:
            raise Unmodelled(f"core instruction {name}")

    @staticmethod
    def _nibs(f: dict) -> tuple:
        return tuple((f[f"{x}_base"], f[f"{x}_stride"]) for x in "abcd")

    # -------------------------------------------------------------- math
    def _scalar(self, sel: int, reg: int) -> float:
        bits = self.s[reg & 15] if sel == A.SRC_S else self.k[reg & 3]
        return from_bits(bits)

    def _math(self, op: str, f: dict) -> None:
        om = bool(f["om"])
        xm, xk, sh, xc, pm, pr = self.xb if om else (A.XM_NONE, 0, 0, 0, 0, 0)
        nibs = self.chk if om else ((0, 1),) * 4
        beats = -(-self.vl // LANES)
        lanes = np.arange(LANES)
        for i in range(beats):
            live = min(LANES, self.vl - LANES * i)
            ch = [(b + s * i) % CHUNKS for b, s in nibs]
            ra = self.v[f["va"], ch[0]].copy()
            rb = self.v[f["vb"], ch[1]].copy()
            rc = self.v[f["vc"], ch[2]].copy()
            if xm == A.XM_MERGE:
                swap = (lanes & xk) != 0
                pa, z = np.where(swap, rb, ra), np.where(swap, ra, rb)
            else:
                pa, z = ra, rb
            if xm in (A.XM_XOR, A.XM_MERGE):
                xq = z[lanes ^ xk]
            elif xm == A.XM_ROT:
                xq = z[(lanes + xk) % LANES]
            elif xm == A.XM_BCAST:
                xq = np.full(LANES, z[(xk + (i >> sh)) % LANES])
            else:
                xq = z
            a = (
                pa
                if f["sa"] == A.SRC_V
                else np.full(LANES, self._scalar(f["sa"], f["va"]))
            )
            b = (
                xq
                if f["sb"] == A.SRC_V
                else np.full(LANES, self._scalar(f["sb"], f["vb"]))
            )
            if f["sc"] != A.SRC_V:
                c = np.full(LANES, self._scalar(f["sc"], f["vc"]))
            else:
                c = xq if xc else rc
            out, pred, ovf = _alu(op, a, b, c)
            d = ch[3]
            mask = lanes < live
            if op in ("vcmplt", "vcmpgt", "vcmpeq"):
                self.p[pr, d, mask] = pred[mask]
                continue
            if pm == 1:
                mask &= self.p[pr, d]
            elif pm == 2:
                mask &= ~self.p[pr, d]
            self.v[f["vd"], d, mask] = out[mask]
            if op == "vexp2d":
                self.sticky |= bool(ovf[mask].any())

    # ------------------------------------------------------ unpack / pack
    def _word(self, w: int) -> bytes:
        w %= L1_WORDS
        return bytes(self.l1[w * WORD : (w + 1) * WORD])

    def _put(self, w: int, data: bytes) -> None:
        w %= L1_WORDS
        self.l1[w * WORD : (w + 1) * WORD] = data

    def _walk(self, name: str, f: dict) -> None:
        unpack = name == "VUNPK"
        p = "U" if unpack else "P"
        base = f["off"]
        if f["rel"]:
            base += self.ptr[f"{p}PTR"]
            self.ptr[f"{p}PTR"] = (self.ptr[f"{p}PTR"] + self.ptr[f"{p}INC"]) % L1_WORDS
        s0, s1 = self.stride[f"{p}STR"]

        def addr(w: int) -> int:
            return base + (w % 4) * s0 + (w // 4) * s1

        reg, mode, n, cb = f["reg"], f["mode"], f["n1"] + 1, f["cb"]
        if mode == A.MX7:
            if unpack:
                raise Unmodelled("an MX7 unpack (F_MODE)")
            # Rows r = 0..3: chunks 2r, 2r+1, 32 values; one quantiser entry.
            rows = self.v[reg].reshape(4, 2 * LANES).astype(np.float32)
            entry = tensor.pack(rows, 1, 1, cb & 1)
            for w in range(4):
                self._put(addr(w), entry[w * WORD : (w + 1) * WORD])
            return
        if mode == A.GT4 and n % 4:
            raise Unmodelled(f"a GT4 walk of {n} chunks (F_GT4)")
        for i in range(n):
            chunk = (cb + i) % CHUNKS
            if mode == A.F16:
                if unpack:
                    raw = np.frombuffer(self._word(addr(i)), np.float16)
                    self.v[reg, chunk] = raw.astype(np.float64)
                else:
                    self._put(addr(i), self.v[reg, chunk].astype(np.float16).tobytes())
            elif mode == A.F32:
                if unpack:
                    raw = self._word(addr(2 * i)) + self._word(addr(2 * i + 1))
                    self.v[reg, chunk] = e8(np.frombuffer(raw, np.float32))
                else:
                    raw = self.v[reg, chunk].astype(np.float32).tobytes()
                    self._put(addr(2 * i), raw[:WORD])
                    self._put(addr(2 * i + 1), raw[WORD:])
            elif i % 4 == 0:
                self._gt4(reg, cb, i, addr, unpack)

    def _gt4(self, reg: int, cb: int, i: int, addr, unpack: bool) -> None:
        """Four words <-> four chunks, transposed as 4 x 4 granules of 64 bits
        (out word j = granule j of in words 0..3)."""
        chunks = [(cb + i + j) % CHUNKS for j in range(4)]
        if unpack:
            g = np.stack(
                [
                    np.frombuffer(self._word(addr(i + w)), np.float16).reshape(4, 4)
                    for w in range(4)
                ]
            )  # [word, granule, value]
            for j, ch in enumerate(chunks):
                self.v[reg, ch] = g[:, j, :].reshape(-1).astype(np.float64)
        else:
            vals = np.stack(
                [self.v[reg, ch].astype(np.float16).reshape(4, 4) for ch in chunks]
            )
            for w in range(4):
                self._put(addr(i + w), np.ascontiguousarray(vals[:, w, :]).tobytes())

    # --------------------------------------------------------------- DMA
    def _dma(self, name: str, f: dict, at: int) -> None:
        if name == "VDRAIN" and (f["node"] or f["dst_x"] or f["dst_y"]):
            self._peer(f, at)
            return
        addrs = self._words(f["ad"], at)
        off = f["off"]
        for w, a in enumerate(addrs):
            if name == "VFILL":
                self._put(off + w, self.mem.read_block(a, WORD))
            else:
                self.mem.write_block(a, self._word(off + w))

    def _peer(self, f: dict, at: int) -> None:
        """A peer drain: the walk's word count of L1 words from `off`, one
        burst to (dst_x, dst_y)'s buffer; the descriptor's base (plus a `rel`
        drain's running offset) is {COUNT [34], fin [33:26], mesh [25:24], ack
        {y, x} [23:16], granule [15:0]}."""
        base = self.desc.get((f["ad"], 0), 0) + at
        if (base >> 26) & 0xFF:
            raise Unmodelled("a peer drain into another mesh")
        n = 1
        for d in (1, 2, 3, 4):
            n *= max(self.desc.get((f["ad"], d), 0) & 0xFFFF, 1)
        data = b"".join(self._word(f["off"] + w) for w in range(n))
        ack = ((base >> 16) & 0xF, (base >> 20) & 0xF)
        self.net.burst(
            self.at[0],
            (f["dst_x"], f["dst_y"]),
            f["buf_id"],
            base & 0xFFFF,
            data,
            count=bool((base >> 34) & 1),
            signal=bool(f["signal"]),
            ack=ack,
        )

    def _words(self, ad: int, at: int) -> list:
        """The walk's word addresses: base + at + sum idx_d * stride_d, dim 0
        innermost, a bound of 0 one step."""
        addr = [self.desc.get((ad, 0), 0) + at]
        for d in (1, 2, 3, 4):
            v = self.desc.get((ad, d), 0)
            stride, bound = _signed((v >> 16) & 0x3FFFF, 18), max(v & 0xFFFF, 1)
            addr = [a + k * stride for k in range(bound) for a in addr]
        return addr


def _alu(op: str, a, b, c) -> tuple:
    """``(result, compare bit, EXP2D overflow)`` of one op on lane arrays."""
    nan = np.isnan(a) | np.isnan(b)
    if op in ("vcmplt", "vcmpgt", "vcmpeq"):
        eq = (a == b) & ~nan
        lt = (a < b) & ~nan
        gt = (a > b) & ~nan
        pred = {"vcmplt": lt, "vcmpgt": gt, "vcmpeq": eq}[op]
        return np.where(pred, 1.0, 0.0), pred, None
    with np.errstate(all="ignore"):
        if op == "vmov":
            out = a
        elif op == "vneg":
            out = -a
        elif op == "vabs":
            out = np.abs(a)
        elif op == "vadd":
            out = a + c
        elif op == "vsub":
            out = a - c
        elif op == "vmul":
            out = a * b
        elif op == "vfma":
            out = a * b + c
        elif op == "vfnma":
            out = -(a * b) + c
        elif op == "vmax":
            out = np.where((a < b) & ~nan, b, a)
        elif op == "vmin":
            out = np.where((a > b) & ~nan, b, a)
        elif op == "vsel":
            out = np.where(c != 0, a, b)
        elif op == "vexp2":
            out = np.exp2(a)
        elif op == "vlog2":
            out = np.log2(a)
        elif op == "vinv":
            out = 1.0 / a
        elif op == "vrsqrt":
            out = 1.0 / np.sqrt(a)
        elif op == "vexp2d":
            out = np.exp2(a - np.floor(b))
        else:
            raise Unmodelled(f"math op {op}")
        out = e8(out) + 0.0  # -0 to +0, as the core's moves do
    ovf = None
    if op == "vexp2d":
        ovf = ~np.isfinite(out) | (np.abs(out) >= 2.0**TAU)
    return out, None, ovf


def _signed(v: int, bits: int) -> int:
    return v - (1 << bits) if v >> (bits - 1) else v


__all__ = ["VectorCore", "from_bits", "to_bits"]
