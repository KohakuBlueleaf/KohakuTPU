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
| `acc[NACC]` | accumulators, `SIMD` int32 lanes -- exactly one vreg wide |
| `vspad` | the vector scratchpad, `VSPAD_ENTRIES` entries of `VW` bits |

The accumulator being **exactly as wide as a vector register** is the design
decision the rest follows from: `vdot` reduces the elements *within* each 32-bit
lane rather than across the whole vector, so a 4-way int8 dot and a 2-way int16
dot both land in one int32 per lane. That is the ARM `SDOT` / x86 `VPDPBUSD`
shape, and it is what makes `vaccrd` a plain move rather than a narrowing.

## The memory map gains one region

`0x4xxx_xxxx` is the vector scratchpad. The base core's four regions are
untouched; a build without the SIMD extension faults on it exactly as it faults
on any other unmapped address.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from rv_model import Machine, Halt, CAUSE_FAULT, MASK, sx           # noqa: E402
import rv_simd_isa as I                                              # noqa: E402
import rv_simd_f16 as F                                              # noqa: E402


def fsfu_e8(base, e8):
    """One seed on one E8M15 element. SPECIALS EXACT, finite by float64.

    The specials are transcribed from tests/vector/vec_alu_tb.v section 9, which
    pins them ON THE RTL and passes -- exp2(-inf)=0, log2(-2)=NaN, inv(-0)=-inf,
    rsqrt(-4)=NaN. `rv_simd_fsfu_test.py` asserts this function against that same
    table, so the two cannot drift apart.

    The FINITE path is a float64 REFERENCE and not bit-exact: vec_alu computes it
    from a 32-segment table plus a range reduction. A bench comparing these on
    finite data must do it by tolerance and name the cases that carry one.

    WHAT USED TO BE HERE, and passed for a long time: a final
    `max(min(y, 3.4e38), -3.4e38)`. It turned EVERY infinity and NaN into
    0x7f7fca00, so the model could not produce an infinity for ANY input -- and
    3.4e38 is not even FP32 max (7f7fc99e against 7f7fffff). It is what made
    rsqrt(-1) a large finite where the hardware correctly returns NaN.
    """
    import math
    import struct

    e, m = (e8 >> 15) & 0xFF, e8 & 0x7FFF
    s, zero, inf, nan = (e8 >> 23) & 1, e == 0, e == 0xFF and m == 0, \
        e == 0xFF and m != 0
    neg = s and not zero                          # -0 is NOT negative here
    f = struct.unpack("<f", struct.pack("<I", F.e8_to_f32(e8)))[0]
    inf_p, inf_n, nan_v = float("inf"), float("-inf"), float("nan")

    if nan:
        y = nan_v
    elif base == "vfexp2":
        # 2**x: +inf saturates up, -inf all the way down to zero.
        if inf:
            y = inf_p if not s else 0.0
        else:
            try:
                y = 2.0 ** f
            except OverflowError:
                # float64 STOPS AT 2^1024 and the exponent field does not: any
                # x above it is +inf, which is what the RTL already returns for
                # e_a > 134. Without this the whole float stream dies in the
                # GENERATOR, so `khs_run.py --float` could not run at all.
                y = inf_p
    elif base == "vflog2":
        y = nan_v if neg else inf_n if zero else inf_p if inf else math.log2(f)
    elif base == "vfrcp":
        # THE SIGN SURVIVES AT BOTH ENDS: vec_alu's OP_INV takes spec_sign_c
        # from the input's sign, so 1/-inf is -0 and not +0. vec_alu_tb section 9
        # tests inv(+inf) and not inv(-inf), so nothing else pins this.
        y = (inf_n if s else inf_p) if zero else \
            (-0.0 if s else 0.0) if inf else 1.0 / f
    elif base == "vfrsqrt":
        # THE SIGN SURVIVES THROUGH ZERO: 5.4.1 gives squareRoot(-0) = -0 and
        # 1/-0 is -inf, so rsqrt(-0) is -inf, as OpenCL and CUDA also specify.
        # This read `inf_p if zero` -- transcribed from vec_alu's OP_RSQRT, which
        # hardcodes spec_sign_c = 1'b0 and is wrong on that one input.
        y = (nan_v if neg else (inf_n if s else inf_p) if zero
             else 0.0 if inf else 1.0 / math.sqrt(f))
    else:
        raise ValueError("not a seed: %s" % base)

    if y != y:
        return F.E8_NAN
    try:
        bits = struct.unpack("<I", struct.pack("<f", y))[0]
    except OverflowError:                         # IEEE overflows to INFINITY
        bits = 0xFF800000 if y < 0 else 0x7F800000
    return F.f32_to_e8(bits)

VSPAD_BASE = 0x4000_0000
R_VSPAD = 5                     # beside rv_model's R_SPAD..R_DRAM


def _sat(v, bits):
    lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    return lo if v < lo else hi if v > hi else v


class DspMachine(Machine):
    """One SIMD PE's architectural state: the base core's, plus the vector file."""

    def __init__(self, simd=8, vregs=I.VREGS, nacc=I.NACC,
                 vspad_entries=1024, npart=16, flanes=None, **kw):
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
        # HOW MANY FLOAT UNITS THE BUILD HAS, against 2*SIMD elements. **0 means
        # NOT BUILT**, the same spelling khs_unit and the SIMT PE now use; it
        # used to mean "one per element", so the same 0 described opposite
        # machines. The count is ARCHITECTURAL for the same reason NPART is:
        # with fewer units an element's chain is npart/passes partials instead
        # of npart, so the accumulation order -- and therefore the answer --
        # differs. A model that ignored it would not be bit-exact.
        # UNSPECIFIED IS NOT ZERO. An explicit 0 is "not built"; omitting the
        # argument gets the widest tier, so the callers that never cared about
        # the count are unchanged and only a deliberate 0 turns the tier off.
        self.flanes = (2 * simd) if flanes is None else flanes
        self.fpasses = (2 * simd) // self.flanes if self.flanes else 0
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
    # alignment (rv_simd_f16.py). Modelling the definition instead would make
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

    def _f32slots(self, val):
        """The same register read as SIMD FP32 elements."""
        return [(val >> (32 * j)) & 0xFFFF_FFFF for j in range(self.simd)]

    # FP32 ELEMENT j OCCUPIES SLOT 2j, and that is the whole of the difference.
    # An even lane takes its own 16-bit slot and its neighbour's as one FP32
    # while the odd lane idles, so the pass an element rides on, the partial its
    # chain lives in and the order the fold walks them are the SAME FUNCTIONS OF
    # THE SLOT INDEX in both formats -- only the conversion at the edge differs.
    def _fin(self, val, wide):
        """(slot index, E8M15 word) for every element of a source register.

        The slots FP32 does not own are ZERO, not absent: the partials are one
        memory word per turn, so a lane with nothing to do still writes -- and a
        zero factor is what makes that write the addend it just read.
        """
        if not wide:
            return [(i, F.f16_to_e8(x)) for i, x in enumerate(self._fslots(val))]
        out = [(i, 0) for i in range(2 * self.simd)]
        for j, x in enumerate(self._f32slots(val)):
            out[2 * j] = (2 * j, F.f32_to_e8(x))
        return out

    def _fold(self, parts):
        """The partials, combined in index order -- once per reduction."""
        tot = 0
        for p in parts:
            tot = F.e8_fma_hw(p, F.f16_to_e8(0x3C00), tot)
        return tot

    # ---- FALU, FCVT, FSFU: the elementwise groups ------------------------
    # PACKED, unlike the accumulator above. A register is vw/w elements at the
    # operand width -- 16 f16 or 8 f32 -- which is the integer tier's rule and
    # every CPU SIMD ISA's. The SIMT PE places one element per 32-bit slot
    # instead because there a slot is a thread; the arithmetic is identical
    # either way, so the two agree element for element.
    def _fpack_in(self, val, w):
        m = (1 << w) - 1
        cv = F.f16_to_e8 if w == 16 else F.f32_to_e8
        return [cv((val >> (w * i)) & m) for i in range(self.vw // w)]

    def _fpack_out(self, e8s, w):
        cv = F.e8_to_f16 if w == 16 else F.e8_to_f32
        out = 0
        for i, e in enumerate(e8s):
            out |= (cv(e) & ((1 << w) - 1)) << (w * i)
        return out & self.vmask

    @staticmethod
    def _e8_nan(x):
        return ((x >> 15) & 0xFF) == 0xFF and (x & 0x7FFF) != 0

    @staticmethod
    def _e8_key(x):
        """Sign-magnitude as an orderable integer. +0 and -0 both key to 0."""
        mag = x & 0x7FFFFF
        return -mag if (x >> 23) & 1 else mag

    def _exec_falu(self, base, w, o, pc):
        one = F.f16_to_e8(0x3C00)
        a = self._fpack_in(self.v[o["vs1"]], w)
        b = self._fpack_in(self.v[o["vs2"]], w)
        d = self._fpack_in(self.v[o["vd"]], w)
        n = self.vw // w
        allset = (1 << w) - 1
        r, mask_out = [], 0

        for i in range(n):
            x, y, z = a[i], b[i], d[i]
            if base == "vfmul":
                r.append(F.e8_fma_hw(x, y, 0))
            elif base == "vfadd":
                r.append(F.e8_fma_hw(x, one, y))
            elif base == "vfsub":
                r.append(F.e8_fma_hw(x, one, y ^ (1 << 23)))
            elif base == "vfma":
                r.append(F.e8_fma_hw(x, y, z))
            elif base in ("vfmin", "vfmax"):
                # vec_alu.v:177 `OP_MAX: va = cmp_lt ? s1_b : s1_a`, and MIN the
                # same on cmp_gt. A NaN makes both compares false, so the winner
                # is VS1 -- taken from the RTL rather than chosen here, because
                # the lane is shipped silicon and this model follows it.
                if self._e8_nan(x) or self._e8_nan(y):
                    win = x
                else:
                    ka, kb = self._e8_key(x), self._e8_key(y)
                    win = y if ((ka < kb) if base == "vfmax" else (ka > kb)) else x
                r.append(F.e8_fma_hw(win, one, 0))
            else:
                if self._e8_nan(x) or self._e8_nan(y):
                    hit = False
                else:
                    ka, kb = self._e8_key(x), self._e8_key(y)
                    hit = {"vfcmplt": ka < kb, "vfcmpgt": ka > kb,
                           "vfcmpeq": ka == kb}[base]
                mask_out |= (allset if hit else 0) << (w * i)

        if base.startswith("vfcmp"):
            self.v[o["vd"]] = mask_out & self.vmask
        else:
            self.v[o["vd"]] = self._fpack_out(r, w)
        return None

    def _exec_fcvt(self, kind, w, o, pc):
        n = self.vw // w
        src = self.v[o["vs1"]]
        m = (1 << w) - 1

        if kind == "f2i":
            # One int32 per float element, so an f16 source would fill two
            # registers: the LOW SIMD elements only, exactly as vunpkl narrows.
            out = 0
            for i in range(min(n, self.simd)):
                e8 = (F.f16_to_e8 if w == 16 else F.f32_to_e8)((src >> (w * i)) & m)
                out |= (self._e8_trunc_i32(e8) & MASK) << (32 * i)
            self.v[o["vd"]] = out & self.vmask
            return None

        if kind == "i2f":
            out = 0
            for i in range(min(n, self.simd)):
                v = sx((src >> (32 * i)) & MASK, 32)
                e8 = self._i32_to_e8(v)
                cv = F.e8_to_f16 if w == 16 else F.e8_to_f32
                out |= (cv(e8) & m) << (w * i)
            self.v[o["vd"]] = out & self.vmask
            return None

        # f2f: the element type names the DESTINATION, and the count halves or
        # doubles, so this is the float vunpkl / vpack.
        out = 0
        if w == 32:                                   # widen f16 -> f32
            for j in range(self.simd):
                out |= (F.e8_to_f32(F.f16_to_e8((src >> (16 * j)) & 0xFFFF))
                        & 0xFFFF_FFFF) << (32 * j)
        else:                                         # narrow f32 -> f16
            for j in range(self.simd):
                out |= (F.e8_to_f16(F.f32_to_e8((src >> (32 * j)) & 0xFFFF_FFFF))
                        & 0xFFFF) << (16 * j)
        self.v[o["vd"]] = out & self.vmask
        return None

    @staticmethod
    def _e8_trunc_i32(e8):
        """E8M15 -> int32, toward zero, saturating. A NaN gives zero."""
        s, e, sig, kind = F.e8_parts(e8)
        if kind in ("zero", "nan"):
            return 0
        if kind == "inf":
            return -(1 << 31) if s else (1 << 31) - 1
        sh = (e - 127) - 15
        v = (sig << sh) if sh >= 0 else (sig >> -sh)
        v = -v if s else v
        lo, hi = -(1 << 31), (1 << 31) - 1
        return lo if v < lo else hi if v > hi else v

    @staticmethod
    def _i32_to_e8(v):
        """int32 -> E8M15, round to nearest even."""
        if v == 0:
            return 0
        s = 1 if v < 0 else 0
        mag = abs(v)
        k = mag.bit_length() - 1
        if k >= 15:
            sig = mag >> (k - 15)
            guard = (mag >> (k - 16)) & 1 if k >= 16 else 0
            stick = 1 if (k >= 17 and (mag & ((1 << (k - 16)) - 1))) else 0
        else:
            sig, guard, stick = mag << (15 - k), 0, 0
        if guard & (stick | (sig & 1)):
            sig += 1
            if sig >> 16:
                sig >>= 1
                k += 1
        return (s << 23) | ((k + 127) << 15) | (sig & 0x7FFF)

    def _exec_fsfu(self, base, w, o, pc):
        a = self._fpack_in(self.v[o["vs1"]], w)
        self.v[o["vd"]] = self._fpack_out([fsfu_e8(base, x) for x in a], w)
        return None

    def _exec_float(self, base, suf, o, pc):
        # `vfcvt` spells its direction before its type: vfcvt.f2i.f16.
        kind = None
        if "." in suf:
            kind, _, suf = suf.partition(".")
        if suf not in ("f16", "f32", ""):
            raise Halt(CAUSE_FAULT, pc)

        if kind is not None:
            return self._exec_fcvt(kind, 32 if suf == "f32" else 16, o, pc)
        w = 32 if suf == "f32" else 16
        if base in ("vfmul", "vfadd", "vfsub", "vfma", "vfmin", "vfmax",
                    "vfcmplt", "vfcmpgt", "vfcmpeq"):
            return self._exec_falu(base, w, o, pc)
        if base in ("vfexp2", "vflog2", "vfrcp", "vfrsqrt"):
            return self._exec_fsfu(base, w, o, pc)
        # FP32 needs two narrow lanes to carry one element, so a one-lane float
        # tier has no FP32 encoding at all -- khs_unit faults on it.
        wide = suf == "f32"
        if wide and self.flanes < 2:
            raise Halt(CAUSE_FAULT, pc)
        ad = o.get("ad", o.get("as1"))

        if base == "vfaccz":
            self.facc[ad] = [[0] * self.npart for _ in range(2 * self.simd)]
            self.fturn[ad] = 0
            return None
        if base in ("vfmacc", "vfmsac"):
            # The subtract flips each element's SIGN BIT, which moves with the
            # format: bit 31 of an FP32 element, bit 15 of an FP16 one.
            w = 32 if wide else 16
            neg = (sum(1 << (w * n + w - 1) for n in range(self.vw // w))
                   if base == "vfmsac" else 0)
            a = self._fin(self.v[o["vs1"]], wide)
            b = self._fin(self.v[o["vs2"]] ^ neg, wide)
            k = self.fturn[ad]
            for (i, ae), (_, be) in zip(a, b):
                # Element i is handled on pass i//flanes, and the turn counter
                # advances once per PASS -- so each pass owns the partials
                # congruent to it, and the passes never collide.
                idx = (k + i // self.flanes) % self.npart
                self.facc[ad][i][idx] = F.e8_fma_hw(ae, be,
                                                    self.facc[ad][i][idx])
            self.fturn[ad] = (k + self.fpasses) % self.npart
            return None
        if base == "vfaccwr":
            # The seed for element i lands on ITS pass's first partial, not on
            # partial 0 -- partial 0 belongs to pass 0 alone. Slots FP32 leaves
            # idle are zeroed, as the sweep's own word zeroes them.
            self.facc[ad] = [[0] * self.npart for _ in range(2 * self.simd)]
            for i, e in self._fin(self.v[o["vs1"]], wide):
                self.facc[ad][i][i // self.flanes] = e
            self.fturn[ad] = 0
            return None
        if base == "vfaccrd":
            out = 0
            step = 2 if wide else 1
            for i in range(0, 2 * self.simd, step):
                # Only this element's own chain: the partials congruent to its
                # pass, in the order the fold walks them.
                parts = self.facc[ad][i]
                p = i // self.flanes
                mine = [parts[k * self.fpasses + p]
                        for k in range(self.npart // self.fpasses)]
                tot = self._fold(mine)
                out |= (F.e8_to_f32(tot) if wide
                        else F.e8_to_f16(tot)) << (16 * i)
            self.v[o["vd"]] = out
            return None
        if base == "vfredsum":
            tot = 0
            for i in range(0, 2 * self.simd, 2 if wide else 1):
                tot = F.e8_fma_hw(self._fold(self.facc[ad][i]),
                                  F.f16_to_e8(0x3C00), tot)
            return (F.e8_to_f32(tot) if wide else F.e8_to_f16(tot)) & MASK
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
