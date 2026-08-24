"""A golden SIMT model: W resident waves, L lanes, active masks, an IPDOM stack.

The RTL is co-simulated against this one instruction at a time, and the
comparison is per LANE: for every instruction that commits, the bench checks the
PC, the destination and the value in every lane. A core that keeps its
arithmetic and loses its masks is wrong here on the instruction it happened to.

It also counts what the coalescer must do, because "requests issued per gather"
is a witness and not a statistic -- see `Wave.pending` and `GpuMachine.reqs`.

## The register-class rule

RV32I opcode space addresses the PER-THREAD file, so `x5` is L values. The
scalar file `s0`-`s31` is per wave and is reached only through custom-2/3.
`ballot` and `redux` are the only vector-to-scalar path; there is no raw move,
because under a mask it would have to invent which lane wins.

## The IPDOM contract, stated exactly because the design note does not

`split` pushes TWO entries and `join` pops ONE:

    split vs1:  t = mask & pred ; f = mask & ~pred
                push(mask)          <- restores the outer mask (reconvergence)
                push(f)             <- the false path
                mask = t ; pc += 4
    join:       mask = pop() ; pc += 4

A popped entry carries no PC: the resume point is always the popping `join`'s
own `pc + 4`. That is what makes the entry L bits wide instead of L + 32, and it
is exact for structured control flow, which SPIR-V guarantees by naming a merge
block for every selection and loop.

Two consequences a frontend must honour:

* **Every `split` needs exactly two `join`s**, so an `if` with no `else` emits an
  empty else block. Balanced by construction beats balanced by analysis.
* **A depth bound of D permits D/2 nested divergent levels, not D-1.** Each
  split costs two entries. At the default D = 8 that is four levels; seven needs
  D = 16. Overflow is a FAULT -- never a wrap, never a mask merge.
"""

MASK = 0xFFFFFFFF

# G9's arithmetic is the SIMD tier's, so its MODEL is too: `e8_fma_hw` reproduces
# the built lane rather than the correctly-rounded definition, which is the only
# way a golden model can be bit-exact against `vec_alu`.
from rv_simd_f16 import (
    e8_fma_hw,
    e8_to_f16,
    e8_to_f32,
    f16_to_e8,
    f32_to_e8,
)

F16_ONE, F16_ZERO = 0x3C00, 0x0000
F32_ONE = 0x3F80_0000

# Regions, as the SIMT PE's MEM stage decodes them. LDS replaces the SIMD tier's
# vector scratchpad at the same address, because it is the same array.
SPAD_BASE = 0x1000_0000
CTL_BASE = 0x2000_0000
LDS_BASE = 0x4000_0000
DRAM_BASE = 0x8000_0000

R_SPAD, R_CTL, R_LDS, R_DRAM, R_BAD = 1, 2, 4, 8, 7

CAUSE_ECALL, CAUSE_EBREAK, CAUSE_FAULT = 1, 2, 3

OPC_KHG, OPC_KHGI = 0x5B, 0x7B

#: One 32-byte line is one memory entry, one flit and one L1 line, so it is also
#: the unit the coalescer groups by.
LINE_BYTES = 32


def sx(v, bits):
    v &= (1 << bits) - 1
    return v - (1 << bits) if v & (1 << (bits - 1)) else v


def region(a):
    if a & 0x8000_0000:
        return R_DRAM
    return {1: R_SPAD, 2: R_CTL, 4: R_LDS}.get((a >> 28) & 7, R_BAD)


class Halt(Exception):
    def __init__(self, cause, word):
        super().__init__("halt cause %d word 0x%08x" % (cause, word))
        self.cause = cause
        self.word = word


class Wave:
    """One resident wave: a PC, a mask, a per-lane register file, a scalar file."""

    def __init__(self, wid, lanes, pc=0, mask=None, depth=8):
        self.wid = wid
        self.lanes = lanes
        self.pc = pc
        self.mask = (1 << lanes) - 1 if mask is None else mask
        self.x = [[0] * lanes for _ in range(32)]
        self.s = [0] * 32
        self.stack = []
        self.depth = depth
        self.done = False
        self.hi_water = 0

    def active(self):
        return [i for i in range(self.lanes) if (self.mask >> i) & 1]

    def push(self, m):
        if len(self.stack) >= self.depth:
            raise Halt(CAUSE_FAULT, self.pc)
        self.stack.append(m)
        self.hi_water = max(self.hi_water, len(self.stack))

    def pop(self):
        if not self.stack:
            raise Halt(CAUSE_FAULT, self.pc)
        return self.stack.pop()


def coalesce(addrs):
    """Leader/follower, over 32-byte lines. Returns the list of lines served.

    The forward-progress property the loop rests on: every pass serves at least
    its own leader, so it terminates in at most len(addrs) passes. The bench
    asserts that in simulation rather than trusting this comment.
    """
    todo = dict(enumerate(addrs))
    lines = []
    while todo:
        lead = min(todo)
        line = todo[lead] & ~(LINE_BYTES - 1)
        served = [i for i, a in todo.items() if (a & ~(LINE_BYTES - 1)) == line]
        assert lead in served, "a pass that does not serve its own leader"
        lines.append((line, served))
        for i in served:
            del todo[i]
    return lines


class GpuMachine:
    """The PE's architectural state, and the memory its shaders can see."""

    def __init__(
        self,
        lanes=8,
        waves=16,
        imem_words=2048,
        lds_words=8192,
        depth=8,
        ctl=None,
        nlive=1,
    ):
        self.lanes = lanes
        self.imem = [0] * imem_words
        self.lds = [0] * lds_words
        self.spad = [0] * 2048
        self.dram = {}
        self.ctl = list(ctl or [0] * 32)
        # `waves` is what the build CARRIES; `nlive` is what this kick launches,
        # clamped to it. The two are different questions and conflating them is
        # how "16 waves" ends up meaning storage in one place and issue in
        # another.
        self.waves = [Wave(i, lanes, depth=depth) for i in range(waves)]
        for w in self.waves[max(1, min(nlive, waves)) :]:
            w.done = True
        self.reqs = []
        self.instret = 0
        self.cause = 0
        self.halt_word = 0

    # ---- memory ---------------------------------------------------------
    def _word(self, a, pc):
        r = region(a)
        if r == R_LDS:
            return self.lds[((a & 0x0FFF_FFFF) >> 2) % len(self.lds)]
        if r == R_SPAD:
            return self.spad[((a & 0x0FFF_FFFF) >> 2) % len(self.spad)]
        if r == R_DRAM:
            return self.dram.get(a & ~3, 0)
        if r == R_CTL:
            return 0
        raise Halt(CAUSE_FAULT, pc)

    def _store(self, a, val, be, pc):
        r = region(a)
        base = a & ~3
        if r == R_LDS:
            arr, i = self.lds, ((a & 0x0FFF_FFFF) >> 2) % len(self.lds)
        elif r == R_SPAD:
            arr, i = self.spad, ((a & 0x0FFF_FFFF) >> 2) % len(self.spad)
        elif r == R_DRAM:
            arr, i = self.dram, base
        else:
            raise Halt(CAUSE_FAULT, pc)
        old = arr.get(i, 0) if isinstance(arr, dict) else arr[i]
        new = 0
        for b in range(4):
            src = val if (be >> b) & 1 else old
            new |= ((src >> (8 * b)) & 0xFF) << (8 * b)
        arr[i] = new & MASK

    def load(self, a, width, signed, pc):
        if width == 2 and (a & 1):
            raise Halt(CAUSE_FAULT, pc)
        if width == 4 and (a & 3):
            raise Halt(CAUSE_FAULT, pc)
        w = self._word(a, pc)
        if width == 1:
            v = (w >> (8 * (a & 3))) & 0xFF
            return sx(v, 8) & MASK if signed else v
        if width == 2:
            v = (w >> (16 * ((a >> 1) & 1))) & 0xFFFF
            return sx(v, 16) & MASK if signed else v
        return w & MASK

    def store(self, a, val, width, pc):
        if width == 2 and (a & 1):
            raise Halt(CAUSE_FAULT, pc)
        if width == 4 and (a & 3):
            raise Halt(CAUSE_FAULT, pc)
        if width == 1:
            self._store(a, (val & 0xFF) * 0x0101_0101, 1 << (a & 3), pc)
        elif width == 2:
            self._store(
                a, (val & 0xFFFF) * 0x0001_0001, 0b1100 if (a & 2) else 0b0011, pc
            )
        else:
            self._store(a, val, 0xF, pc)

    # ---- one instruction, across the active lanes ------------------------
    def step(self, w):
        pc = w.pc
        if (pc >> 2) >= len(self.imem):
            raise Halt(CAUSE_FAULT, pc)
        ins = self.imem[pc >> 2]
        opc = ins & 0x7F
        w.pc = (pc + 4) & MASK
        self.instret += 1
        if opc == OPC_KHG:
            return self._khg(w, ins, pc)
        if opc == OPC_KHGI:
            return self._khgi(w, ins, pc)
        return self._rv32(w, ins, pc)

    def _rv32(self, w, ins, pc):
        """Ordinary RV32I, applied to every ACTIVE lane of the per-thread file."""
        rd = (ins >> 7) & 0x1F
        f3 = (ins >> 12) & 7
        rs1 = (ins >> 15) & 0x1F
        rs2 = (ins >> 20) & 0x1F
        f7 = (ins >> 25) & 0x7F
        opc = ins & 0x7F
        imm_i = sx(ins >> 20, 12)
        imm_s = sx(((ins >> 25) << 5) | ((ins >> 7) & 0x1F), 12)
        imm_u = ins & 0xFFFF_F000
        imm_j = sx(
            (((ins >> 31) & 1) << 20)
            | (((ins >> 12) & 0xFF) << 12)
            | (((ins >> 20) & 1) << 11)
            | (((ins >> 21) & 0x3FF) << 1),
            21,
        )

        # A per-thread condition reaching one PC is undefined, so the encoding
        # refuses it rather than promising the compiler proved uniformity.
        if opc == 0x63:
            raise Halt(CAUSE_FAULT, pc)
        if opc == 0x73:
            if (ins >> 21) or ((ins >> 7) & 0x1FFF):
                raise Halt(CAUSE_FAULT, pc)
            # ONE WAVE RETIRES, NOT THE UNIT. A dispatch is finished when its
            # LAST wave is; a fault, by contrast, is a property of the program
            # and still kills everything. The halt word and cause are the last
            # retiring wave's, which is what the hardware's a0 snoop records.
            w.done = True
            self.cause = CAUSE_EBREAK if (ins >> 20) & 1 else CAUSE_ECALL
            self.halt_word = w.x[10][0]
            return (pc, 0, {})
        if opc == 0x0F:
            return None

        vals = {}
        for ln in w.active():
            a, b = w.x[rs1][ln], w.x[rs2][ln]
            if opc == 0x37:
                v = imm_u
            elif opc == 0x17:
                v = (pc + imm_u) & MASK
            elif opc == 0x6F:
                v = (pc + 4) & MASK
                w.pc = (pc + imm_j) & MASK
            elif opc == 0x67:
                v = (pc + 4) & MASK
                w.pc = (a + imm_i) & MASK & ~1
            elif opc == 0x03:
                if f3 in (3, 6, 7):
                    raise Halt(CAUSE_FAULT, pc)
                ad = (a + imm_i) & MASK
                self._note(ad, w, ln)
                v = self.load(ad, {0: 1, 1: 2, 2: 4, 4: 1, 5: 2}[f3], f3 in (0, 1), pc)
            elif opc == 0x23:
                if f3 > 2:
                    raise Halt(CAUSE_FAULT, pc)
                ad = (a + imm_s) & MASK
                self._note(ad, w, ln)
                self.store(ad, b, 1 << f3, pc)
                v = None
            elif opc == 0x33 and f7 == 0x01:
                # RV32M. ONE 33x33 SIGNED PRODUCT SERVES ALL FOUR: only the
                # extension bits differ, and `mul`'s low half does not depend on
                # them at all. Getting the sign extension wrong here is the easy
                # mistake, so the three high forms are spelled out separately.
                if f3 > 3:
                    raise Halt(CAUSE_FAULT, pc)  # div/rem stay illegal
                sa, sb = sx(a, 32), sx(b, 32)
                if f3 == 0:
                    v = (sa * sb) & MASK  # mul: low half
                elif f3 == 1:
                    v = ((sa * sb) >> 32) & MASK  # mulh: signed x signed
                elif f3 == 2:
                    v = ((sa * b) >> 32) & MASK  # mulhsu: signed x unsigned
                else:
                    v = ((a * b) >> 32) & MASK  # mulhu: unsigned
            elif opc in (0x13, 0x33):
                second = imm_i & MASK if opc == 0x13 else b
                sh = (imm_i & 0x1F) if opc == 0x13 else (b & 0x1F)
                sub = opc == 0x33 and f7 == 0x20
                if f3 == 0:
                    v = (a - b) & MASK if sub else (a + second) & MASK
                elif f3 == 1:
                    v = (a << sh) & MASK
                elif f3 == 2:
                    v = 1 if sx(a, 32) < sx(second, 32) else 0
                elif f3 == 3:
                    v = 1 if a < second else 0
                elif f3 == 4:
                    v = a ^ second
                elif f3 == 5:
                    v = (sx(a, 32) >> sh) & MASK if f7 == 0x20 else (a >> sh)
                elif f3 == 6:
                    v = a | second
                else:
                    v = a & second
            else:
                raise Halt(CAUSE_FAULT, pc)
            if v is not None:
                vals[ln] = v & MASK

        if rd != 0:
            for ln, v in vals.items():
                w.x[rd][ln] = v
            return (pc, rd, dict(vals))
        return (pc, 0, {})

    def _note(self, addr, w, ln):
        """Record one lane's address so a gather's request count is countable."""
        if self.reqs and self.reqs[-1][0] == (w.wid, w.pc):
            self.reqs[-1][1].append(addr)
        else:
            self.reqs.append(((w.wid, w.pc), [addr]))

    def _khg(self, w, ins, pc):
        """custom-2: the R-type groups."""
        rd = (ins >> 7) & 0x1F
        f3 = (ins >> 12) & 7
        rs1 = (ins >> 15) & 0x1F
        rs2 = (ins >> 20) & 0x1F
        f7 = (ins >> 25) & 0x7F

        if f3 == 0:  # SALU
            a, b = w.s[rs1], w.s[rs2]
            v = {
                0: (a + b) & MASK,
                1: (a - b) & MASK,
                2: (a << (b & 31)) & MASK,
                3: 1 if sx(a, 32) < sx(b, 32) else 0,
                4: 1 if a < b else 0,
                5: a ^ b,
                6: a >> (b & 31),
                7: (sx(a, 32) >> (b & 31)) & MASK,
                8: a | b,
                9: a & b,
            }.get(f7)
            if v is None:
                raise Halt(CAUSE_FAULT, pc)
            if rd:
                w.s[rd] = v & MASK
            return (pc, -rd, w.s[rd] if rd else 0)

        if f3 == 1:  # SMOV
            if f7 == 0:
                for ln in w.active():
                    w.x[rd][ln] = w.s[rs1]
                return (pc, rd, {ln: w.s[rs1] for ln in w.active()})
            if f7 == 1:
                # Slot 5 is THIS WAVE'S id, so it cannot come from the flat
                # control table -- it is the only way a wave learns which of the
                # dispatch it is, and every per-wave address derives from it.
                if rd:
                    w.s[rd] = (w.wid if rs2 == 5 else self.ctl[rs2]) & MASK
                return (pc, -rd, w.s[rd] if rd else 0)
            raise Halt(CAUSE_FAULT, pc)

        if f3 == 2:  # DIV
            if f7 == 0:
                t = sum(1 << ln for ln in w.active() if w.x[rs1][ln] != 0)
                f = w.mask & ~t
                w.push(w.mask)
                w.push(f)
                w.mask = t
                return (pc, 0, {})
            if f7 == 1:
                w.mask = w.pop()
                return (pc, 0, {})
            if f7 == 2:
                w.mask = w.s[rs1] & ((1 << self.lanes) - 1)
                if w.mask == 0:
                    w.done = True
                return (pc, 0, {})
            if f7 == 3:
                return (pc, 0, {})
            raise Halt(CAUSE_FAULT, pc)

        if f3 == 3:  # SUB
            act = w.active()
            if f7 == 0:
                src = {ln: w.x[rs1][ln] for ln in act}
                out = {
                    ln: src.get(ln ^ (w.s[rs2] & (self.lanes - 1)), w.x[rs1][ln])
                    for ln in act
                }
                for ln, v in out.items():
                    w.x[rd][ln] = v
                return (pc, rd, out)
            if f7 == 1:
                v = w.x[rs1][rs2 % self.lanes]
                for ln in act:
                    w.x[rd][ln] = v
                return (pc, rd, {ln: v for ln in act})
            if f7 == 2:
                v = sum(1 << ln for ln in act if w.x[rs1][ln] != 0)
                if rd:
                    w.s[rd] = v
                return (pc, -rd, v)
            if f7 == 9:
                for ln in act:
                    w.x[rd][ln] = ln
                return (pc, rd, {ln: ln for ln in act})
            if f7 == 8:
                # The LOWEST ACTIVE lane, never lane 0. An all-zero mask is not
                # a defined case; the scheduler must not issue such a wave.
                assert act, "vreadfirst issued on an all-zero active mask"
                v = w.x[rs1][act[0]]
                if rd:
                    w.s[rd] = v
                return (pc, -rd, v)
            vals = [sx(w.x[rs1][ln], 32) for ln in act]
            if f7 == 3:
                v = sum(vals) & MASK
            elif f7 == 4:
                v = (max(vals) if vals else 0) & MASK
            elif f7 == 5:
                v = (min(vals) if vals else 0) & MASK
            elif f7 == 6:
                v = MASK
                for ln in act:
                    v &= w.x[rs1][ln]
            elif f7 == 7:
                v = 0
                for ln in act:
                    v |= w.x[rs1][ln]
            else:
                raise Halt(CAUSE_FAULT, pc)
            if rd:
                w.s[rd] = v & MASK
            return (pc, -rd, v & MASK)

        if f3 == 4:  # VMEM
            op, scale, width = (f7 >> 4) & 7, (f7 >> 2) & 3, f7 & 3
            if width > 2 or op > 5:
                raise Halt(CAUSE_FAULT, pc)
            nby = 1 << width
            lin = op >= 3
            store = op in (2, 5)
            signed = op in (0, 3)
            # Offsets are SIGNED and the sum wraps at 32 bits, both pinned.
            addrs = {
                ln: (w.s[rs1] + ((ln if lin else sx(w.x[rs2][ln], 32)) << scale)) & MASK
                for ln in w.active()
            }
            if addrs:
                self.reqs.append(((w.wid, pc), list(addrs.values())))
            if store:
                for ln, ad in addrs.items():
                    self.store(ad, w.x[rd][ln], nby, pc)
                return (pc, 0, {})
            out = {ln: self.load(ad, nby, signed, pc) for ln, ad in addrs.items()}
            for ln, v in out.items():
                w.x[rd][ln] = v
            return (pc, rd, out)

        if f3 == 5:  # FLT (G9)
            # THE MODEL IS THE DSP TIER'S, NOT A SECOND ONE. `e8_fma_hw` is the
            # lane as built -- alignment window, clamped shift, uncomplemented
            # sticky and all -- so this is bit-exact against the hardware for
            # the same reason the arithmetic was not forked.
            # funct7[3] IS THE FORMAT and funct7[2:0] the operation: 0-3 the
            # arithmetic four, 4-7 the FSFU seeds, +8 for the FP16 form of each.
            # This read `funct7[2]` as the format and refused everything above 7
            # -- the encoding from before the seeds took a bit -- so it faulted
            # on every `_h` operation while the RTL computed it. It was invisible
            # because kht_sys_tb was reading a stale image: see its `simt/user`.
            if f7 > 15:
                raise Halt(CAUSE_FAULT, pc)
            # The seeds are HAS_FSFU, which is OFF in the build this models, and
            # the PE faults on one rather than returning what FMA made of it.
            if f7 & 4:
                raise Halt(CAUSE_FAULT, pc)
            half = bool(f7 & 8)
            cvt_in = f16_to_e8 if half else f32_to_e8
            cvt_out = e8_to_f16 if half else e8_to_f32
            mask = 0xFFFF if half else MASK
            one = F16_ONE if half else F32_ONE
            sign = 0x8000 if half else 0x8000_0000
            out = {}
            for ln in w.active():
                av = w.x[rs1][ln] & mask
                bv = w.x[rs2][ln] & mask
                dv = w.x[rd][ln] & mask
                op = f7 & 3
                if op == 1:  # vfmul
                    sa, sb, sc = av, bv, 0
                elif op == 2:  # vfadd
                    sa, sb, sc = av, one, bv
                elif op == 3:  # vfsub
                    sa, sb, sc = av, one, bv ^ sign
                else:  # vfma
                    sa, sb, sc = av, bv, dv
                y = cvt_out(e8_fma_hw(cvt_in(sa), cvt_in(sb), cvt_in(sc)))
                # On the FP16 path element 1 is RESERVED and reads back zero, so
                # a shader that ignores the contract gets a defined value rather
                # than whatever the integer lanes last left in the upper half.
                out[ln] = y & mask
            for ln, v in out.items():
                w.x[rd][ln] = v
            return (pc, rd, out)

        raise Halt(CAUSE_FAULT, pc)

    def _khgi(self, w, ins, pc):
        """custom-3: the I-type groups. Scalar, so one value and no mask."""
        rd = (ins >> 7) & 0x1F
        f3 = (ins >> 12) & 7
        rs1 = (ins >> 15) & 0x1F
        imm = sx(ins >> 20, 12)
        a = w.s[rs1]
        if f3 == 6:
            if a == 0:
                w.pc = (pc + imm) & MASK
            return (pc, 0, 0)
        if f3 == 7:
            if a != 0:
                w.pc = (pc + imm) & MASK
            return (pc, 0, 0)
        v = {
            0: (a + imm) & MASK,
            1: a & (imm & MASK),
            2: a | (imm & MASK),
            3: (a << (imm & 31)) & MASK,
            4: a >> (imm & 31),
            5: (sx(a, 32) >> (imm & 31)) & MASK,
        }[f3]
        if rd:
            w.s[rd] = v & MASK
        return (pc, -rd, v & MASK)

    # ---- the scheduler ---------------------------------------------------
    def run(self, limit=2_000_000):
        """Round-robin over ready waves until one halts or all are done.

        Ready-wave interleaving, not a compulsory barrel period: with four ready
        waves the model issues from four, not from sixteen with twelve bubbles.
        """
        trace = []
        try:
            for _ in range(limit):
                live = [w for w in self.waves if not w.done]
                if not live:
                    return trace, self.cause or CAUSE_ECALL, self.halt_word
                for w in live:
                    trace.append((w.wid,) + self.step(w))
            raise RuntimeError("model did not halt within %d steps" % limit)
        except Halt as h:
            self.cause = h.cause
            self.halt_word = h.word
            return trace, h.cause, h.word

    def fills_issued(self):
        """Distinct 32-byte lines the recorded accesses would have requested."""
        return sum(len(coalesce(a)) for _, a in self.reqs)
