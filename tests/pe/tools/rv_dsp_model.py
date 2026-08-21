"""The KohakuDSP golden model: exact semantics for every tier-1 instruction.

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
| `acc[NACC]` | accumulators, `SIMD` int32 lanes -- exactly one vreg wide |
| `vspad` | the vector scratchpad, `VSPAD_ENTRIES` entries of `VW` bits |

The accumulator being **exactly as wide as a vector register** is the design
decision the rest follows from: `vdot` reduces the elements *within* each 32-bit
lane rather than across the whole vector, so a 4-way int8 dot and a 2-way int16
dot both land in one int32 per lane. That is the ARM `SDOT` / x86 `VPDPBUSD`
shape, and it is what makes `vaccrd` a plain move rather than a narrowing.

## The memory map gains one region

`0x4xxx_xxxx` is the vector scratchpad. The base core's four regions are
untouched; a build without the DSP extension faults on it exactly as it faults
on any other unmapped address.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from rv_model import Machine, Halt, CAUSE_FAULT, MASK, sx           # noqa: E402
import rv_dsp_isa as I                                              # noqa: E402
import rv_dsp_f16 as F                                              # noqa: E402

VSPAD_BASE = 0x4000_0000
R_VSPAD = 5                     # beside rv_model's R_SPAD..R_DRAM


def _sat(v, bits):
    lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    return lo if v < lo else hi if v > hi else v


class DspMachine(Machine):
    """One DSP PE's architectural state: the base core's, plus the vector file."""

    def __init__(self, simd=8, vregs=I.VREGS, nacc=I.NACC,
                 vspad_entries=1024, npart=16, **kw):
        super().__init__(**kw)
        self.simd = simd
        self.vw = 32 * simd
        self.vmask = (1 << self.vw) - 1
        self.vregs = vregs
        self.nacc = nacc
        self.v = [0] * vregs
        self.acc = [[0] * simd for _ in range(nacc)]
        # The float accumulator: 2*SIMD slots of NPART E8M15 partials each, and
        # a per-accumulator turn counter. See _exec_float -- the rotation is
        # architectural, not an implementation detail.
        self.npart = npart
        self.facc = [[[0] * npart for _ in range(2 * simd)] for _ in range(nacc)]
        self.fturn = [0] * nacc
        self.vspad = [0] * vspad_entries
        self.vcount = {}            # name -> dynamic count, for the frontier

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
        self.vspad[e] = ((self.vspad[e] & ~(MASK << (32 * k)))
                         | ((new & MASK) << (32 * k)))

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
    # `e8_fma_hw`, NOT `e8_fma`: the model has to be what the MACHINE does, and
    # the lane carries a one-ulp deviation from correct rounding on subtractive
    # alignment (rv_dsp_f16.py). Modelling the definition instead would make
    # every bit-exact comparison fail on 0.5% of real data and call the hardware
    # wrong for matching its own arithmetic.
    #
    # E8M15 throughout, the vector core's format, and its accumulation shape:
    # each of the 2*SIMD slots holds NPART PARTIALS, and consecutive accumulate
    # operations land on successive partials. That is what breaks the FMA's
    # 14-cycle recurrence in `vec_lanes`, and it is ARCHITECTURAL rather than a
    # hardware detail -- float addition does not associate, so a model that
    # accumulated serially into one partial would compute a different answer
    # from the machine. The order is part of the contract.
    def _fslots(self, val):
        """A vector register read as 2*SIMD FP16 elements."""
        return [(val >> (16 * i)) & 0xFFFF for i in range(2 * self.simd)]

    def _fold(self, parts):
        """The partials, combined in index order -- once per reduction."""
        tot = 0
        for p in parts:
            tot = F.e8_fma_hw(p, F.f16_to_e8(0x3C00), tot)
        return tot

    def _exec_float(self, base, suf, o, pc):
        if suf not in ("f16", ""):
            raise Halt(CAUSE_FAULT, pc)
        ad = o.get("ad", o.get("as1"))

        if base == "vfaccz":
            self.facc[ad] = [[0] * self.npart for _ in range(2 * self.simd)]
            self.fturn[ad] = 0
            return None
        if base in ("vfmacc", "vfmsac"):
            a = self._fslots(self.v[o["vs1"]])
            b = self._fslots(self.v[o["vs2"]])
            k = self.fturn[ad]
            for i in range(2 * self.simd):
                bi = b[i] ^ 0x8000 if base == "vfmsac" else b[i]
                self.facc[ad][i][k] = F.e8_fma_hw(F.f16_to_e8(a[i]),
                                               F.f16_to_e8(bi),
                                               self.facc[ad][i][k])
            self.fturn[ad] = (k + 1) % self.npart
            return None
        if base == "vfaccwr":
            src = self._fslots(self.v[o["vs1"]])
            self.facc[ad] = [[F.f16_to_e8(s)] + [0] * (self.npart - 1)
                             for s in src]
            self.fturn[ad] = 0
            return None
        if base == "vfaccrd":
            out = 0
            for i, parts in enumerate(self.facc[ad]):
                out |= F.e8_to_f16(self._fold(parts)) << (16 * i)
            self.v[o["vd"]] = out
            return None
        if base == "vfredsum":
            tot = 0
            for parts in self.facc[ad]:
                tot = F.e8_fma_hw(self._fold(parts), F.f16_to_e8(0x3C00), tot)
            return F.e8_to_f16(tot) & MASK
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
                "vand": a & b, "vor": a | b, "vxor": a ^ b,
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

        # ---- dot product and the accumulators ----
        if base in ("vdot", "vdotn"):
            per = 32 // bits
            a = self._elems(self.v[o["vs1"]], bits)
            b = self._elems(self.v[o["vs2"]], bits)
            acc = self.acc[o["ad"]]
            for j in range(self.simd):
                s = sum(a[j * per + k] * b[j * per + k] for k in range(per))
                acc[j] = sx((acc[j] + (s if base == "vdot" else -s)) & MASK, 32)
            return None
        if base == "vaccz":
            self.acc[o["ad"]] = [0] * self.simd
            return None
        if base == "vaccrd":
            self.v[o["vd"]] = self._from_lanes(self.acc[o["as1"]])
            return None
        if base == "vaccwr":
            self.acc[o["ad"]] = self._lanes(self.v[o["vs1"]])
            return None

        # ---- scalar moves and reductions ----
        if base == "vsplat":
            self.v[o["vd"]] = self._pack([self.x[o["xs1"]]] * self.simd, 32)
            return None
        if base == "vextr":
            return self._lanes(self.v[o["vs1"]])[o["sh"] % self.simd] & MASK
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
                [cat[(k + i) % n] for i in range(self.simd)])
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
                self.vspad[e] = ((self.vspad[e] & ~(MASK << (32 * k)))
                                 | ((w & MASK) << (32 * k)))
