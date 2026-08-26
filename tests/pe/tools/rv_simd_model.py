"""The KohakuSIMD golden model: exact semantics for every tier-1 instruction.

`DspMachine` is `rv_model.Machine` plus vector state, reached through the base
model's one extension hook, so the base ISA's behaviour is untouched and the
existing gates keep verifying the same core.

**Everything here is exact integer arithmetic.** Saturation bounds, wrapping
widths and the accumulator's width are semantics, not tolerances -- a model that
compared within an epsilon would accept an accumulator one bit too narrow, which
is the failure this whole layer exists to catch.

## The vector state

| | |
|---|---|
| `v[VREGS]` | vector registers, `VW = 32 * SIMD` bits each, held as ints |
| `facc[NACC]` | the FLOAT accumulator: `SIMD` slots of `NPART` binary32 partials |
| `vspad` | the vector scratchpad, `VSPAD_ENTRIES` entries of `VW` bits |

There is no integer dot unit and no integer accumulator. A dot product is a
packed `vmul` followed by `vredsum`, or a multiply whose partials the scalar
core sums; the float accumulator below is a separate structure.

## The memory map gains one region

`0x4xxx_xxxx` is the vector scratchpad. The base core's four regions are
untouched; a build without the SIMD extension faults on it exactly as it faults
on any other unmapped address.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import khs_fp32 as K
import khs_seed_tab as SEED
import rv_simd_isa as I
from rv_model import CAUSE_FAULT, MASK, Halt, Machine, sx

#: The FALU stem -> the rv_fpu opcode that implements it. `vfadd` is the FMA
#: with its multiplier forced to one and `vfmul` the FMA with its addend forced
#: to zero, which is what the RTL does rather than a second datapath.
FALU_OP = {
    "vfmul": K.OP_MUL,
    "vfadd": K.OP_ADD,
    "vfsub": K.OP_SUB,
    "vfma": K.OP_FMA,
    "vfmin": K.OP_MIN,
    "vfmax": K.OP_MAX,
    "vfcmplt": K.OP_CMPLT,
    "vfcmpgt": K.OP_CMPGT,
    "vfcmpeq": K.OP_CMPEQ,
}

VSPAD_BASE = 0x4000_0000
R_VSPAD = 5  # beside rv_model's R_SPAD..R_DRAM


def _sat(v, bits):
    lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    return lo if v < lo else min(v, hi)


class DspMachine(Machine):
    """One SIMD PE's architectural state: the base core's, plus the vector file."""

    def __init__(
        self,
        simd=8,
        vregs=I.VREGS,
        nacc=I.NACC,
        vspad_entries=1024,
        npart=16,
        flanes=None,
        **kw,
    ):
        super().__init__(**kw)
        self.simd = simd
        self.vw = 32 * simd
        self.vmask = (1 << self.vw) - 1
        self.vregs = vregs
        self.nacc = nacc
        self.v = [0] * vregs
        # The float accumulator: 2*SIMD slots of NPART E8M15 partials each, and
        # a per-accumulator turn counter. See _exec_float -- the rotation is
        # architectural, not an implementation detail.
        self.npart = npart
        # HOW MANY FLOAT UNITS THE BUILD HAS, against SIMD elements. **0 means
        # NOT BUILT**, the same spelling khs_unit and the SIMT PE now use. The
        # count is ARCHITECTURAL for the same reason NPART is: with fewer units
        # an element's chain is npart/passes partials instead of npart, so the
        # accumulation order -- and therefore the answer -- differs.
        self.flanes = simd if flanes is None else flanes
        self.fpasses = simd // self.flanes if self.flanes else 0
        self.facc = [[[0] * npart for _ in range(simd)] for _ in range(nacc)]
        self.fturn = [0] * nacc
        self.vspad = [0] * vspad_entries
        self.vcount = {}  # name -> dynamic count, for the frontier

    # ---- the vector scratchpad in the scalar map -------------------------
    # STORE ONLY, exactly like a peer window: a scalar store stages data for
    # `vld` to read, and a scalar load faults rather than buying a read port on
    # the base core's critical path.
    def region_of(self, a):
        if (a & 0xF000_0000) == VSPAD_BASE:
            return R_VSPAD
        return super().region_of(a)

    def _store_word(self, a, val, be):
        if self.region_of(a) != R_VSPAD:
            return super()._store_word(a, val, be)
        i = (a & 0x0FFF_FFFF) >> 2
        e, k = divmod(i, self.simd)
        if e >= len(self.vspad):
            raise Halt(CAUSE_FAULT, self.pc)
        old = (self.vspad[e] >> (32 * k)) & MASK
        new = 0
        for b in range(4):
            src = val if (be >> b) & 1 else old
            new |= ((src >> (8 * b)) & 0xFF) << (8 * b)
        self.vspad[e] = (self.vspad[e] & ~(MASK << (32 * k))) | (
            (new & MASK) << (32 * k)
        )

    # ---- element access -------------------------------------------------
    def _elems(self, val, bits):
        n = self.vw // bits
        m = (1 << bits) - 1
        return [sx((val >> (i * bits)) & m, bits) for i in range(n)]

    def _pack(self, elems, bits):
        m = (1 << bits) - 1
        out = 0
        for i, e in enumerate(elems):
            out |= (e & m) << (i * bits)
        return out & self.vmask

    def _lanes(self, val):
        return [sx((val >> (32 * i)) & MASK, 32) for i in range(self.simd)]

    def _from_lanes(self, lanes):
        return self._pack(lanes, 32)

    # ---- the extension hook --------------------------------------------
    def custom(self, opc, ins, pc):
        if opc not in (I.OPC_KHD, I.OPC_KHF):
            raise Halt(CAUSE_FAULT, pc)
        d = I.decode(ins)
        if d is None:
            raise Halt(CAUSE_FAULT, pc)
        name, o = d
        self.vcount[name] = self.vcount.get(name, 0) + 1
        return self._exec(name, o, pc)

    # ---- the float tier -------------------------------------------------
    # `khs_fp32.fpu` IS `rv_fpu`, stage for stage: the model has to be what the
    # MACHINE does, and the unit carries a one-ulp deviation from correct
    # rounding on a subtractive alignment. Modelling the definition instead
    # would fail every bit-exact comparison that hit one and call the hardware
    # wrong for matching its own arithmetic.
    #
    # BINARY32 THROUGHOUT, and the accumulator's shape: each of the SIMD slots
    # holds NPART PARTIALS, and consecutive accumulates land on successive
    # partials. That is what breaks the FMA's recurrence, and it is
    # ARCHITECTURAL -- float addition does not associate, so a model that
    # accumulated serially into one partial would compute a different answer
    # from the machine. The order is part of the contract.
    def _fslots(self, val):
        """A vector register read as SIMD binary32 elements."""
        return [(val >> (32 * i)) & MASK for i in range(self.simd)]

    def _fold(self, parts):
        """The partials, combined in index order -- once per reduction."""
        tot = 0
        for p in parts:
            tot = K.fpu(K.OP_FMA, p, K.F32_ONE, tot)[0]
        return tot

    def _fpack_out(self, words):
        out = 0
        for i, w in enumerate(words):
            out |= (w & MASK) << (32 * i)
        return out & self.vmask

    def _exec_falu(self, base, o, pc):
        a = self._fslots(self.v[o["vs1"]])
        b = self._fslots(self.v[o["vs2"]])
        d = self._fslots(self.v[o["vd"]])
        op = FALU_OP[base]
        # add and sub take their second operand on the ADDEND port; fma takes
        # the destination there.
        wants_b = base in ("vfadd", "vfsub")
        r, mask_out = [], 0
        for i in range(self.simd):
            y, pred = K.fpu(op, a[i], b[i], b[i] if wants_b else d[i])
            if base.startswith("vfcmp"):
                mask_out |= (MASK if pred else 0) << (32 * i)
            else:
                r.append(y)
        if base.startswith("vfcmp"):
            self.v[o["vd"]] = mask_out & self.vmask
        else:
            self.v[o["vd"]] = self._fpack_out(r)

    def _exec_fcvt(self, kind, o, pc):
        src = self._fslots(self.v[o["vs1"]])
        if kind == "f2i":
            self.v[o["vd"]] = self._fpack_out([K.f2i(x) & MASK for x in src])
        else:
            self.v[o["vd"]] = self._fpack_out([K.i2f(x) for x in src])

    def _exec_fsfu(self, base, o, pc):
        a = self._fslots(self.v[o["vs1"]])
        fsel = K.SEED_OF[base]
        self.v[o["vd"]] = self._fpack_out([K.seed(fsel, x, SEED.TAB) for x in a])

    def _exec_float(self, base, suf, o, pc):
        # `vfcvt` spells its direction before its type: vfcvt.f2i.f32.
        kind = None
        if "." in suf:
            kind, _, suf = suf.partition(".")
        if suf not in ("f32", ""):
            raise Halt(CAUSE_FAULT, pc)

        if kind is not None:
            return self._exec_fcvt(kind, o, pc)
        if base in FALU_OP:
            return self._exec_falu(base, o, pc)
        if base in K.SEED_OF:
            return self._exec_fsfu(base, o, pc)
        ad = o.get("ad", o.get("as1"))

        if base == "vfaccz":
            self.facc[ad] = [[0] * self.npart for _ in range(self.simd)]
            self.fturn[ad] = 0
            return None
        if base in ("vfmacc", "vfmsac"):
            neg = (
                sum(1 << (32 * n + 31) for n in range(self.simd))
                if base == "vfmsac"
                else 0
            )
            a = self._fslots(self.v[o["vs1"]])
            b = self._fslots(self.v[o["vs2"]] ^ neg)
            k = self.fturn[ad]
            for i in range(self.simd):
                # Element i is handled on pass i//flanes, and the turn counter
                # advances once per PASS -- so each pass owns the partials
                # congruent to it, and the passes never collide.
                idx = (k + i // self.flanes) % self.npart
                self.facc[ad][i][idx] = K.fpu(
                    K.OP_FMA, a[i], b[i], self.facc[ad][i][idx]
                )[0]
            self.fturn[ad] = (k + self.fpasses) % self.npart
            return None
        if base == "vfaccwr":
            # The seed for element i lands on ITS pass's first partial, not on
            # partial 0 -- partial 0 belongs to pass 0 alone.
            self.facc[ad] = [[0] * self.npart for _ in range(self.simd)]
            for i, e in enumerate(self._fslots(self.v[o["vs1"]])):
                self.facc[ad][i][i // self.flanes] = e
            self.fturn[ad] = 0
            return None
        if base == "vfaccrd":
            out = []
            for i in range(self.simd):
                # Only this element's own chain: the partials congruent to its
                # pass, in the order the fold walks them.
                parts = self.facc[ad][i]
                p = i // self.flanes
                mine = [
                    parts[k * self.fpasses + p]
                    for k in range(self.npart // self.fpasses)
                ]
                out.append(self._fold(mine))
            self.v[o["vd"]] = self._fpack_out(out)
            return None
        if base == "vfredsum":
            tot = 0
            for i in range(self.simd):
                tot = K.fpu(K.OP_FMA, self._fold(self.facc[ad][i]), K.F32_ONE, tot)[0]
            return tot & MASK
        raise Halt(CAUSE_FAULT, pc)

    def _exec(self, name, o, pc):
        base, _, suf = name.partition(".")
        bits = {"s8": 8, "s16": 16, "s32": 32}.get(suf)

        for k in ("vd", "vs1", "vs2", "vs"):
            if k in o and o[k] >= self.vregs:
                raise Halt(CAUSE_FAULT, pc)
        for k in ("ad", "as1"):
            if k in o and o[k] >= self.nacc:
                raise Halt(CAUSE_FAULT, pc)

        if base.startswith("vf"):
            return self._exec_float(base, suf, o, pc)

        # ---- memory ----
        if base == "vld":
            a = (self.x[o["xs1"]] + o["imm"]) & MASK
            self.v[o["vd"]] = self._vread(a, pc)
            return None
        if base == "vst":
            a = (self.x[o["xs1"]] + o["imm"]) & MASK
            self._vwrite(a, self.v[o["vs"]], pc)
            return None

        # ---- element-wise integer ----
        if base in ("vadd", "vsub", "vsadd", "vssub", "vmin", "vmax", "vmul"):
            a = self._elems(self.v[o["vs1"]], bits)
            b = self._elems(self.v[o["vs2"]], bits)
            f = {
                "vadd": lambda p, q: p + q,
                "vsub": lambda p, q: p - q,
                "vsadd": lambda p, q: _sat(p + q, bits),
                "vssub": lambda p, q: _sat(p - q, bits),
                "vmin": min,
                "vmax": max,
                "vmul": lambda p, q: p * q,
            }[base]
            self.v[o["vd"]] = self._pack([f(p, q) for p, q in zip(a, b)], bits)
            return None

        # ---- bitwise, whole vector ----
        if base in ("vand", "vor", "vxor", "vandn"):
            a, b = self.v[o["vs1"]], self.v[o["vs2"]]
            self.v[o["vd"]] = {
                "vand": a & b,
                "vor": a | b,
                "vxor": a ^ b,
                "vandn": a & ~b,
            }[base] & self.vmask
            return None

        # ---- immediate shifts ----
        if base in ("vslli", "vsrli", "vsrai", "vsrari"):
            # The shift amount is masked to the ELEMENT width, as RV32 masks a
            # scalar shift to 5 bits. A packed shift of 20 on int8 elements is
            # otherwise a different instruction on every implementation.
            sh = o["sh"] & (bits - 1)
            a = self._elems(self.v[o["vs1"]], bits)
            m = (1 << bits) - 1
            if base == "vslli":
                r = [(p << sh) for p in a]
            elif base == "vsrli":
                r = [((p & m) >> sh) for p in a]
            elif base == "vsrai":
                r = [(p >> sh) for p in a]
            else:
                # Round to nearest, half away from zero at the LSB: add half an
                # ulp BEFORE the shift. A plain vsrai truncates toward negative
                # infinity, which biases a requantised tensor downward.
                half = (1 << (sh - 1)) if sh else 0
                r = [((p + half) >> sh) for p in a]
            self.v[o["vd"]] = self._pack(r, bits)
            return None

        # THE INTEGER DOT AND ITS ACCUMULATOR ARE NOT BUILT. A dot product is
        # `vmul` then `vredsum`; the encodings fault in `khs_unit`, so a stream
        # containing one is a generator defect and must not be modelled here.

        # ---- scalar moves and reductions ----
        if base == "vsplat":
            self.v[o["vd"]] = self._pack([self.x[o["xs1"]]] * self.simd, 32)
            return None
        if base == "vextr":
            # NOT `% self.simd`. A wrapped lane index makes the same encoding
            # name different elements on different builds; khs_unit faults on an
            # out-of-range one, so an IndexError here is the model saying the
            # same thing rather than quietly agreeing with a wrap.
            return self._lanes(self.v[o["vs1"]])[o["sh"]] & MASK
        if base == "vredsum":
            return sum(self._lanes(self.v[o["vs1"]])) & MASK
        if base == "vredmax":
            return max(self._lanes(self.v[o["vs1"]])) & MASK

        # ---- permute ----
        if base.startswith("vsldw"):
            k = int(base[5:])
            cat = self._lanes(self.v[o["vs1"]]) + self._lanes(self.v[o["vs2"]])
            n = 2 * self.simd
            # A ROTATE of the concatenation, so every index is defined. A
            # clamped or wrapping-to-zero form leaves a hole the RTL and the
            # model would each have to guess the same way.
            self.v[o["vd"]] = self._from_lanes(
                [cat[(k + i) % n] for i in range(self.simd)]
            )
            return None
        if base == "vpack":
            src = 16 if suf == "s16" else 32
            dst = src // 2
            a = self._elems(self.v[o["vs1"]], src)
            b = self._elems(self.v[o["vs2"]], src)
            self.v[o["vd"]] = self._pack([_sat(e, dst) for e in a + b], dst)
            return None
        if base in ("vunpkl", "vunpkh"):
            src = 8 if suf == "s8" else 16
            a = self._elems(self.v[o["vs1"]], src)
            half = len(a) // 2
            part = a[:half] if base == "vunpkl" else a[half:]
            self.v[o["vd"]] = self._pack(part, src * 2)
            return None

        raise Halt(CAUSE_FAULT, pc)

    # ---- the vector scratchpad ------------------------------------------
    def _vaddr(self, a, pc):
        if (a & 0xF000_0000) != VSPAD_BASE:
            raise Halt(CAUSE_FAULT, pc)
        nbytes = self.vw // 8
        if a % nbytes:
            raise Halt(CAUSE_FAULT, pc)
        i = (a & 0x0FFF_FFFF) // nbytes
        if i >= len(self.vspad):
            raise Halt(CAUSE_FAULT, pc)
        return i

    def _vread(self, a, pc):
        return self.vspad[self._vaddr(a, pc)]

    def _vwrite(self, a, val, pc):
        self.vspad[self._vaddr(a, pc)] = val & self.vmask

    # ---- host-side helpers the benches and generators use ----------------
    def vspad_words(self):
        """The vector scratchpad as flat 32-bit words, which is how the NoC
        window writer sees it and how a bench preloads it."""
        out = []
        for e in self.vspad:
            for k in range(self.simd):
                out.append((e >> (32 * k)) & MASK)
        return out

    def load_vspad_words(self, words):
        for i, w in enumerate(words):
            e, k = divmod(i, self.simd)
            if e < len(self.vspad):
                self.vspad[e] = (self.vspad[e] & ~(MASK << (32 * k))) | (
                    (w & MASK) << (32 * k)
                )
