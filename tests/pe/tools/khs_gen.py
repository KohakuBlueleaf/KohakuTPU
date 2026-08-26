"""Build the SIMD datapath's component test: instruction streams and golden state.

    python tests/pe/tools/khs_gen.py --simd 8

Writes tests/pe/build/khd/s<SIMD>/{prog,vfin,spinit,spfin,scal,meta}.hex,
which tests/pe/tb/khs_unit_tb.v walks.

PUSH BUGS DOWN. This drives `khs_unit` on its own, with no core around it, so a
wrong saturation bound or a slide that reads the wrong lane fails HERE -- on the
instruction it happened to, against the golden model's exact integers -- rather
than as a wrong checksum at the end of a kernel three levels up.

Each case is a stream of `(instruction, address, xdata)` triples: the address is
what the core's EX adder would have produced for a `vld`/`vst`, and `xdata` is
`rs1` for a `vsplat`. The bench replays them and the final vector file,
accumulators, scratchpad and scalar-result stream are compared word for word.
"""

import argparse
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import rv_simd_isa as I
import rv_simd_isa_f as IF
from rv_asm import to_hex
from rv_simd_model import VSPAD_BASE, DspMachine

ROOT = pathlib.Path(__file__).resolve().parents[3]

VREGS = 8
NACC = 2
VSPAD_ENTRIES = 64  # small: the bench dumps it word for word
MASK32 = 0xFFFF_FFFF

#: Which instructions a configuration carries. A variant is verified AS ITSELF:
#: a stream that used an encoding the build refuses would fault rather than
#: measure, and a stream that avoided one the build HAS would leave it untested.
#: WIDTHS, NOT BOOLEANS. 0 is not built and its encodings are absent from the
#: stream; any nonzero value emits the same stream and grades the same vectors.
FEAT_ALL = {
    "ilanes": 8,
    "shiftu": 8,
    "permu": 8,
    "red": 1,
    "fcvtu": 0,
    "float": False,
}

#: Rotating partials per float accumulator. ARCHITECTURAL -- float addition
#: does not associate, so the model must carry the number the RTL was built with.
NPART = 16

#: Encoded and NOT BUILT: the unit faults on these, so a generator that emitted
#: one would fail the gate for the right reason at the wrong time. `vfredsum`
#: needs a second pass across the slots that does not exist. FCVT left this list
#: when khs_fcvt.v gave it a datapath.
NOT_BUILT = ("vfredsum.f32",)

#: The elementwise operations, in the order the directed case walks them.
FALU_OPS = (
    "vfmul",
    "vfadd",
    "vfsub",
    "vfma",
    "vfmin",
    "vfmax",
    "vfcmplt",
    "vfcmpgt",
    "vfcmpeq",
)
FSFU_OPS = ("vfexp2", "vflog2", "vfrcp", "vfrsqrt")

#: The float edges, which banded random data never reaches: both zeros, a
#: denormal (which FLUSHES), both infinities, a NaN, the largest finite, and the
#: ties the rounder has to break.
F32_CORNERS = (
    0x0000_0000,
    0x8000_0000,
    0x0000_0001,
    0x007F_FFFF,
    0x0080_0000,
    0x3F80_0000,
    0xBF80_0000,
    0x7F7F_FFFF,
    0xFF7F_FFFF,
    0x7F80_0000,
    0xFF80_0000,
    0x7FC0_0000,
    # A tie that rounds DOWN to even and one that rounds UP, and a
    # mantissa whose carry walks the whole field into the exponent.
    0x3F80_0080,
    0x3F80_0180,
    0x3FFF_FFFF,
    0x0080_0080,
)


class Stream:
    """An instruction stream and the model state it produces."""

    def __init__(self, simd, seed=0, feat=None, flanes=0):
        # `flanes` is ARCHITECTURAL, like NPART: fewer units means a shorter
        # partial chain per element, a different accumulation order and a
        # different answer. The vectors must be generated for the build.
        # 0 is NOT BUILT, not "one per element" -- see DspMachine.
        self.m = DspMachine(
            simd=simd,
            vregs=VREGS,
            nacc=NACC,
            vspad_entries=VSPAD_ENTRIES,
            flanes=flanes,
            imem_words=8,
            spad_words=8,
        )
        self.simd = simd
        self.vb = simd * 4  # bytes per vector
        self.rng = random.Random(seed)
        self.prog = []  # (instr, addr, xdata)
        self.scalars = []
        self.writes = []  # (index, name, vd, value)
        self.feat = feat or FEAT_ALL
        for i in range(32):
            self.m.x[i] = 0
        # Every vector address is built from one base register, and NOTHING
        # else may write it -- a random stream that assigns to it turns the
        # next vector store into a fault instead of a test.
        self.base_reg = 5
        self.xpool = [r for r in range(1, 8) if r != self.base_reg]
        self.m.x[self.base_reg] = VSPAD_BASE

    def v(self, n):
        """A vector register index this build actually has.

        The directed streams are written for eight; a build with four refuses
        `v7` and the case FAULTS rather than measuring anything, which is how
        every small-register configuration failed its gate.
        """
        return n % VREGS

    def a(self, n):
        return n % NACC

    def has(self, name):
        """Whether this configuration carries the instruction at all."""
        op = I.ISA[name]
        # THE OPCODE MAJOR FIRST. funct3 groups restart at zero in custom-1, so
        # every test below would misread a float instruction as an integer one.
        if op.opcode != I.OPC_KHD:
            if not self.feat.get("float") or name in NOT_BUILT:
                return False
            # The float GROUPS are separate parameters, so a stream must emit
            # only what this build carries -- otherwise the case faults on an
            # encoding instead of measuring it.
            if op.group == IF.F3_FALU and not self.feat.get("falu", True):
                return False
            if op.group == IF.F3_FSFU and not self.feat.get("fsfu", True):
                return False
            if op.group in (IF.F3_FMAC, IF.F3_FRED) and not self.feat.get("facc", True):
                return False
            if op.group == IF.F3_FCVT:
                return bool(self.feat.get("fcvtu"))
            return True
        # A WIDTH AT ZERO IS AN ABSENT ENCODING. A narrower width is NOT: it
        # costs cycles and grades against the same golden vectors, which is what
        # makes one stream test every width of a feature that is built.
        if op.group == I.F3_VSHI and not self.feat["shiftu"]:
            return False
        if op.group == I.F3_VPRM and not self.feat["permu"]:
            return False
        if op.group in (I.F3_VINT, I.F3_VBIT) and not self.feat["ilanes"]:
            return False
        if op.group == I.F3_VMOV and name in ("vredsum", "vredmax"):
            return bool(self.feat["red"])
        # The integer dot group is not built at all; `I.ISA` no longer has it.
        return True

    def seed_vspad(self, float_case=False):
        # BANDED FOR THE FLOAT CASES, not uniform words. One uniform word in 256
        # is an infinity or a NaN, a NaN accumulates to a NaN whatever the
        # hardware does, and after thirty accumulates most slots would be NaN --
        # so a dropped accumulate or a doubled zero-sweep would still compare
        # equal. The edges get their own directed cases instead.
        n = VSPAD_ENTRIES * self.simd
        if float_case:
            words = [self.f32v() for _ in range(n)]
        else:
            words = [self.rng.getrandbits(32) for _ in range(n)]
        self.m.load_vspad_words(words)
        return words

    def f32v(self):
        """One finite binary32, exponent banded so 2^-6 <= |x| < 2^6."""
        return (
            (self.rng.getrandbits(1) << 31)
            | (self.rng.randrange(121, 133) << 23)
            | self.rng.getrandbits(23)
        )

    def emit(self, name, **o):
        # Directed cases reached `emit` straight, putting encodings a width-zero
        # build faults in front of a model that executes them all.
        if not self.has(name):
            return
        # Clamp every register index to what this build has, once, here --
        # rather than at each of the hundred call sites that would have to
        # remember.
        for k in ("vd", "vs1", "vs2", "vs"):
            if k in o:
                o[k] = self.v(o[k])
        for k in ("ad", "as1"):
            if k in o:
                o[k] = self.a(o[k])
        # `vextr`'s `sh` is a LANE INDEX, not a shift amount, and the random
        # streams draw every "imm" operand from 0..31. The model used to define
        # it as `sh % simd`, which makes one encoding mean different elements on
        # different builds -- the ISA knowing the width. It is clamped here and
        # REFUSED by khs_unit, so the two cannot disagree again.
        if name == "vextr" and "sh" in o:
            o["sh"] %= self.simd
        word = I.encode(name, **o)
        addr, xdata = 0, 0
        if name in ("vld", "vst"):
            addr = (self.m.x[o["xs1"]] + o["imm"]) & MASK32
        if name == "vsplat":
            xdata = self.m.x[o["xs1"]] & MASK32
        val = self.m.custom(I.ISA[name].opcode, word, 0)
        if val is not None:
            self.scalars.append(val & MASK32)
        # The vector file's write, if this instruction makes one: the golden
        # half of the writeback probe, so a failure names its own instruction.
        if "vd" in o:
            self.writes.append((len(self.prog), name, o["vd"], self.m.v[o["vd"]]))
        self.prog.append((word, addr, xdata))

    # ---- program shapes ---------------------------------------------------
    def load_all(self):
        """Fill every vector register."""
        for v in range(VREGS):
            self.emit("vld", vd=v, imm=v * self.vb, xs1=self.base_reg)

    def directed(self):
        self.load_all()
        for et in ("s8", "s16", "s32"):
            for op in ("vadd", "vsub", "vsadd", "vssub", "vmin", "vmax"):
                self.emit("%s.%s" % (op, et), vd=0, vs1=1, vs2=2)
                self.emit("%s.%s" % (op, et), vd=3, vs1=0, vs2=0)
        for op in ("vand", "vor", "vxor", "vandn"):
            self.emit(op, vd=4, vs1=1, vs2=2)
        # Shifts at both ends of the range: 0 must be the identity and the
        # largest legal amount must not wrap into the next element.
        for et, w in (("s8", 8), ("s16", 16), ("s32", 32)):
            for sh in (0, 1, w // 2, w - 1):
                for op in ("vslli", "vsrli", "vsrai", "vsrari"):
                    if self.has("%s.%s" % (op, et)):
                        self.emit("%s.%s" % (op, et), vd=5, vs1=1, sh=sh)
        for et in ("s8", "s16"):
            if self.has("vmul.%s" % et):
                self.emit("vmul.%s" % et, vd=6, vs1=1, vs2=2)
        # A DOT PRODUCT WITHOUT A DOT UNIT: multiply, then reduce. This is the
        # sequence that replaces `vdot`, so it is a directed case rather than an
        # incidental one.
        for et in ("s8", "s16"):
            if self.has("vmul.%s" % et) and self.has("vredsum"):
                self.emit("vmul.%s" % et, vd=7, vs1=1, vs2=2)
                self.emit("vredsum", xd=self.xpool[0], vs1=7)
        # Moves and reductions.
        self.m.x[6] = 0x1234_5678
        self.emit("vsplat", vd=2, xs1=6)
        for ln in range(min(8, self.simd)):
            self.emit("vextr", xd=7, vs1=1, sh=ln)
        if self.has("vredsum"):
            self.emit("vredsum", xd=7, vs1=1)
            self.emit("vredmax", xd=7, vs1=1)
        if self.feat["permu"]:
            # Every slide index, so a lane picked from the wrong side shows.
            for k in range(8):
                self.emit("vsldw%d" % k, vd=3, vs1=1, vs2=2)
            self.emit("vpack.s16", vd=4, vs1=1, vs2=2)
            self.emit("vpack.s32", vd=5, vs1=1, vs2=2)
            for op in ("vunpkl.s8", "vunpkh.s8", "vunpkl.s16", "vunpkh.s16"):
                self.emit(op, vd=6, vs1=1)
        # Stores, then loads back from the same rows.
        for v in range(4):
            self.emit(
                "vst", vs=v, imm=(VSPAD_ENTRIES // 2 + v) * self.vb, xs1=self.base_reg
            )
        for v in range(4):
            self.emit(
                "vld", vd=v, imm=(VSPAD_ENTRIES // 2 + v) * self.vb, xs1=self.base_reg
            )

    def saturation(self):
        """Drive the saturating ops onto their bounds, which random data misses."""
        self.load_all()
        for et, pat in (("s8", 0x7F7F7F7F), ("s16", 0x7FFF7FFF), ("s32", 0x7FFFFFFF)):
            self.m.x[6] = pat
            self.emit("vsplat", vd=1, xs1=6)
            self.m.x[6] = 0x01010101 if et == "s8" else 1
            self.emit("vsplat", vd=2, xs1=6)
            self.emit("vsadd.%s" % et, vd=3, vs1=1, vs2=2)  # overflows positive
            self.emit("vadd.%s" % et, vd=4, vs1=1, vs2=2)  # wraps instead
            self.m.x[6] = (
                0x80808080
                if et == "s8"
                else (0x80008000 if et == "s16" else 0x80000000)
            )
            self.emit("vsplat", vd=1, xs1=6)
            self.emit("vssub.%s" % et, vd=5, vs1=1, vs2=2)  # overflows negative
            self.emit("vsub.%s" % et, vd=6, vs1=1, vs2=2)
        # A pack whose sources are outside the narrow range in both directions.
        if self.feat["permu"]:
            self.m.x[6] = 0x7FFF8000
            self.emit("vsplat", vd=1, xs1=6)
            self.m.x[6] = 0x0000FFFF
            self.emit("vsplat", vd=2, xs1=6)
            self.emit("vpack.s16", vd=3, vs1=1, vs2=2)
            self.emit("vpack.s32", vd=4, vs1=1, vs2=2)

    def hazards(self):
        """Back-to-back dependencies, which are what the stall rules are for."""
        self.load_all()
        for _ in range(8):
            self.emit("vadd.s32", vd=1, vs1=2, vs2=3)
            self.emit("vadd.s32", vd=4, vs1=1, vs2=1)  # distance 1
            self.emit("vsub.s32", vd=5, vs1=4, vs2=1)  # distance 1 again
            self.emit("vmul.s16", vd=2, vs1=5, vs2=4)  # the extra cycle
            self.emit("vadd.s16", vd=3, vs1=2, vs2=2)  # reads it at once
            self.emit("vmul.s16", vd=6, vs1=2, vs2=3)  # back-to-back multiplies
            self.emit("vmul.s16", vd=6, vs1=3, vs2=2)  # -- the walk's reset
            if self.has("vredsum"):
                self.emit("vredsum", xd=7, vs1=6)
            self.emit("vst", vs=6, imm=0, xs1=self.base_reg)
            self.emit("vld", vd=7, imm=0, xs1=self.base_reg)  # store then load

    def float_corners(self, sfx="f32"):
        """The float edges, on every path a float operand can enter by.

        Random data never reaches a denormal, an infinity or a NaN. `vsplat` is
        how an exact bit pattern gets into a register at all, so each corner is
        splatted and then driven through the operand path (vfmacc/vfmsac) and
        the seed path (vfaccwr).
        """
        self.load_all()
        vals = F32_CORNERS
        for i, w in enumerate(vals):
            self.m.x[6] = w
            self.emit("vsplat", vd=1, xs1=6)
            self.m.x[6] = vals[(i + 1) % len(vals)]
            self.emit("vsplat", vd=2, xs1=6)
            self.emit("vfaccz", ad=0)
            self.emit("vfmacc.%s" % sfx, ad=0, vs1=1, vs2=2)
            self.emit("vfmsac.%s" % sfx, ad=0, vs1=2, vs2=1)
            self.emit("vfaccrd.%s" % sfx, vd=3, as1=0)
            self.emit("vfaccwr.%s" % sfx, ad=1, vs1=1)
            self.emit("vfaccrd.%s" % sfx, vd=4, as1=1)

    def float_directed(self, sfx="f32"):
        """The float accumulator: zero, accumulate, seed, subtract, read back.

        Every read-back is issued with NO GAP behind the last accumulate, and
        every accumulate with no gap behind a zero. Both are the cases the
        hazards exist for: a float accumulate is still in flight when it
        retires, and a zero sweeps the partials for NPART.
        """
        self.load_all()
        for a in range(NACC):
            self.emit("vfaccz", ad=a)
        # More accumulates than partials, so the rotation WRAPS rather than
        # each partial being written once.
        for i in range(NPART + 12):
            self.emit("vfmacc.%s" % sfx, ad=0, vs1=1 + (i % 3), vs2=4 + (i % 3))
        self.emit("vfaccrd.%s" % sfx, vd=0, as1=0)

        self.emit("vfaccwr.%s" % sfx, ad=1, vs1=2)
        for i in range(NPART // 2):
            self.emit("vfmsac.%s" % sfx, ad=1, vs1=3, vs2=5)
        self.emit("vfaccrd.%s" % sfx, vd=1, as1=1)

        # A zero straight after a stream of accumulates, then one accumulate:
        # a sweep that re-armed would land on top of that accumulate's write.
        self.emit("vfaccz", ad=0)
        self.emit("vfmacc.%s" % sfx, ad=0, vs1=1, vs2=2)
        self.emit("vfaccrd.%s" % sfx, vd=2, as1=0)

        for a in range(NACC):
            self.emit("vfaccz", ad=a)
            for _ in range(3):
                self.emit("vfmacc.%s" % sfx, ad=a, vs1=6, vs2=7)
            self.emit("vfaccrd.%s" % sfx, vd=3 + a, as1=a)

        # Two accumulators interleaved: the turn counter is per accumulator.
        if NACC > 1:
            for a in range(NACC):
                self.emit("vfaccz", ad=a)
            for i in range(NPART + 5):
                self.emit("vfmacc.%s" % sfx, ad=i % NACC, vs1=1, vs2=2 + (i % 2))
            for a in range(NACC):
                self.emit("vfaccrd.%s" % sfx, vd=5 + a, as1=a)

    def falu_directed(self, sfx="f32"):
        """The elementwise group, as a DEPENDENT CHAIN.

        Every instruction reads the one before it, so the scoreboard serialises
        them and the vector-file writes stay in program order -- which is what
        the bench's write trace compares. INDEPENDENT elementwise instructions
        retire OUT of order by construction: the result lands FLOAT_ALAT cycles
        after the instruction leaves MEM, so a short instruction behind it
        reaches the write port first. That is correct and it is not tested
        here, because testing it needs a compare that does not assume order.
        """
        self.load_all()
        for op in FALU_OPS:
            name = "%s.%s" % (op, sfx)
            if not self.has(name):
                continue
            # vd is a source for vfma, so the chain runs through it either way.
            self.emit(name, vd=3, vs1=1, vs2=2)
            # Distance 1 on the float's own destination: the scoreboard's job.
            self.emit("vfadd.%s" % sfx, vd=1, vs1=3, vs2=3)
            self.emit("vfmul.%s" % sfx, vd=2, vs1=1, vs2=3)
        for op in FSFU_OPS:
            name = "%s.%s" % (op, sfx)
            if not self.has(name):
                continue
            self.emit(name, vd=4, vs1=1)
            self.emit("vfadd.%s" % sfx, vd=1, vs1=4, vs2=4)
        # A float write followed by an INTEGER read of the same register: the
        # hazard has to hold across the tiers, not only within the float one.
        if self.has("vfmul.%s" % sfx):
            self.emit("vfmul.%s" % sfx, vd=5, vs1=1, vs2=2)
            # A DEPENDENT READER, so the float is ordered before the vld below.
            # An elementwise float retires late and an INDEPENDENT instruction
            # behind it legitimately overtakes it, which the write trace cannot
            # express. `vadd.s32` did this until ILANES=0 filtered it out and
            # the vld started landing first.
            if self.has("vadd.s32"):
                self.emit("vadd.s32", vd=6, vs1=5, vs2=5)
            else:
                self.emit("vst", vs=5, imm=0, xs1=self.base_reg)
            self.emit("vst", vs=6, imm=0, xs1=self.base_reg)
            self.emit("vld", vd=7, imm=0, xs1=self.base_reg)

    def falu_corners(self, sfx="f32"):
        """The elementwise ops on the float edges, which banded data misses."""
        self.load_all()
        vals = F32_CORNERS
        for i, w in enumerate(vals):
            self.m.x[6] = w
            self.emit("vsplat", vd=1, xs1=6)
            self.m.x[6] = vals[(i + 1) % len(vals)]
            self.emit("vsplat", vd=2, xs1=6)
            for op in ("vfmul", "vfadd", "vfsub", "vfmin", "vfmax", "vfcmplt"):
                name = "%s.%s" % (op, sfx)
                if self.has(name):
                    self.emit(name, vd=3, vs1=1, vs2=2)
                    # Dependent, for the reason falu_directed's tail is. A STORE
                    # rather than another float: it orders without inventing
                    # arithmetic on a compare's mask.
                    if self.has("vadd.s32"):
                        self.emit("vadd.s32", vd=4, vs1=3, vs2=3)
                    else:
                        self.emit("vst", vs=3, imm=0, xs1=self.base_reg)

    def float_random(self, n):
        """The float tier only, on banded data, with the accumulators read out."""
        self.load_all()
        for a in range(NACC):
            self.emit("vfaccz", ad=a)
        pool = [k for k in I.ISA if I.ISA[k].opcode != I.OPC_KHD and self.has(k)]
        for _ in range(n):
            name = self.rng.choice(pool)
            vals = {}
            for o in I.ISA[name].operands:
                if o.kind == "vreg":
                    vals[o.name] = self.rng.randrange(VREGS)
                elif o.kind == "areg":
                    vals[o.name] = self.rng.randrange(NACC)
                else:
                    vals[o.name] = self.rng.randrange(32)
            self.emit(name, **vals)
        for a in range(NACC):
            self.emit("vfaccrd.f32", vd=a, as1=a)

    def random(self, n):
        self.load_all()
        # THE INTEGER TIER ONLY. The float instructions have their own cases,
        # on banded data, for the reason `seed_vspad` gives.
        pool = [
            k
            for k in I.ISA
            if k not in ("vld", "vst") and self.has(k) and I.ISA[k].opcode == I.OPC_KHD
        ]
        for _ in range(n):
            name = self.rng.choice(pool)
            op = I.ISA[name]
            vals = {}
            for o in op.operands:
                if o.kind == "vreg":
                    vals[o.name] = self.rng.randrange(VREGS)
                elif o.kind == "areg":
                    vals[o.name] = self.rng.randrange(NACC)
                elif o.kind == "xreg":
                    r = self.rng.choice(self.xpool)
                    self.m.x[r] = self.rng.getrandbits(32)
                    vals[o.name] = r
                else:
                    vals[o.name] = self.rng.randrange(32)
            self.emit(name, **vals)
            if self.rng.random() < 0.15:
                row = self.rng.randrange(VSPAD_ENTRIES)
                self.emit(
                    "vst",
                    vs=self.rng.randrange(VREGS),
                    imm=row * self.vb,
                    xs1=self.base_reg,
                )
                self.emit(
                    "vld",
                    vd=self.rng.randrange(VREGS),
                    imm=row * self.vb,
                    xs1=self.base_reg,
                )


#: (name, builder, FP16-banded scratchpad)
INT_CASES = [
    ("directed", lambda s: s.directed(), False),
    ("saturate", lambda s: s.saturation(), False),
    ("hazards", lambda s: s.hazards(), False),
    ("random1", lambda s: s.random(300), False),
    ("random2", lambda s: s.random(300), False),
    ("random3", lambda s: s.random(300), False),
]
FLOAT_CASES = [
    ("floatd", lambda s: s.float_directed(), True),
    ("floatr", lambda s: s.float_random(200), True),
    ("f32edge", lambda s: s.float_corners(), True),
    ("falud", lambda s: s.falu_directed(), True),
    ("faluedge", lambda s: s.falu_corners(), True),
]


def cases_for(feat, flanes):
    """`flanes` is the float UNIT COUNT, and 0 means the tier is not built."""
    if not feat.get("float"):
        return INT_CASES
    acc = feat.get("facc", True)
    el = feat.get("falu", True) or feat.get("fsfu", True)
    return INT_CASES + [
        c
        for c in FLOAT_CASES
        if ((c[0].startswith("falu") and el) or (not c[0].startswith("falu") and acc))
    ]


def build(simd, outdir, feat, tag, flanes=0):
    # One fixed directory, because the bench cannot be handed a string: it
    # reads `cur` and verifies the configuration out of meta instead.
    d = pathlib.Path(outdir) / "cur"
    d.mkdir(parents=True, exist_ok=True)
    cases = cases_for(feat, flanes)
    prog, vfin, spinit, spfin, scal, meta = [], [], [], [], [], []
    wtr, names = [], []
    for n, (name, fn, is_float) in enumerate(cases):
        s = Stream(simd, seed=1000 + n, feat=feat, flanes=flanes)
        spinit.append(s.seed_vspad(is_float))
        fn(s)
        wtr.append(s.writes)
        names.append([w[1] for w in s.writes])
        prog.append(s.prog)
        vfin.append(
            [(s.m.v[v] >> (32 * k)) & MASK32 for v in range(VREGS) for k in range(simd)]
        )
        spfin.append(s.m.vspad_words())
        scal.append(s.scalars)
        print(
            "  %-9s %5d instructions, %3d scalar results"
            % (name, len(s.prog), len(s.scalars))
        )

    # One flat file per kind, with each case's block at a fixed stride, so the
    # bench indexes rather than parses.
    ni = max(len(p) for p in prog)
    ns = max(len(x) for x in scal)
    flat_prog, flat_scal = [], []
    for c in range(len(cases)):
        blk = prog[c] + [(0, 0, 0)] * (ni - len(prog[c]))
        for w, a, x in blk:
            flat_prog.append((w << 64) | (a << 32) | x)
        flat_scal += scal[c] + [0] * (ns - len(scal[c]))

    # The writeback trace: one line per vector-file write, `{index, vd, value}`
    # padded to a fixed stride so the bench indexes rather than parses.
    nw = max(len(w) for w in wtr)
    ww = 32 + 8 + 32 * simd
    flat_w = []
    for c in range(len(cases)):
        blk = wtr[c] + [(0, "-", 0, 0)] * (nw - len(wtr[c]))
        for ix, _, vd, val in blk:
            flat_w.append((ix << (8 + 32 * simd)) | (vd << (32 * simd)) | val)
    (d / "wtrace.hex").write_text(
        "".join(("%0" + str((ww + 3) // 4) + "x\n") % w for w in flat_w)
    )
    (d / "wcounts.hex").write_text(to_hex([len(w) for w in wtr]))
    (d / "wnames.txt").write_text(
        "".join(
            "%d\t%d\t%s\n" % (c, i, n)
            for c, ns in enumerate(names)
            for i, n in enumerate(ns)
        )
    )

    (d / "prog.hex").write_text("".join("%024x\n" % w for w in flat_prog))
    (d / "scal.hex").write_text(to_hex(flat_scal))
    (d / "vfin.hex").write_text(to_hex([w for c in vfin for w in c]))
    (d / "spinit.hex").write_text(to_hex([w for c in spinit for w in c]))
    (d / "spfin.hex").write_text(to_hex([w for c in spfin for w in c]))
    # The vectors CARRY their configuration and the bench checks it against its
    # own parameters. A `-d TAG=name` string would have done the same job, but
    # xvlog gives a bare define an identifier rather than a string literal, and
    # a check the bench can make itself is worth more than a path it is told.
    # EVERY WIDTH IS CARRIED, because a width of 0 removes encodings from the
    # stream: vectors built with the shifter cannot grade a build without it.
    # meta[12] is the float LANE count, architectural because it changes the
    # accumulation order.
    meta = [
        len(cases),
        ni,
        ns,
        VREGS,
        NACC,
        VSPAD_ENTRIES,
        simd,
        nw,
        feat["ilanes"],
        feat["shiftu"],
        feat["permu"],
        1 if feat.get("float") else 0,
        flanes,
        feat["red"],
    ]
    (d / "meta.hex").write_text(to_hex(meta))
    (d / "counts.hex").write_text(to_hex([len(p) for p in prog]))
    (d / "scounts.hex").write_text(to_hex([len(x) for x in scal]))
    print("  %d cases, stride %d instructions, into %s" % (len(cases), ni, d))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--simd", type=int, default=8, choices=(2, 4, 8))
    ap.add_argument("--vregs", type=int, default=8)
    ap.add_argument("--nacc", type=int, default=2)
    # WIDTHS, and 0 means the feature is NOT BUILT. A nonzero value below full
    # changes only the cycle count, so it does not change the stream.
    ap.add_argument("--ilanes", type=int, default=8, choices=(0, 1, 2, 4, 8))
    ap.add_argument("--shiftu", type=int, default=8, choices=(0, 1, 2, 4, 8))
    ap.add_argument("--permu", type=int, default=8, choices=(0, 1, 2, 4, 8))
    ap.add_argument("--red", type=int, default=1, choices=(0, 1))
    ap.add_argument("--fcvtu", type=int, default=0, choices=(0, 1, 2, 4, 8))
    ap.add_argument(
        "--float", action="store_true", dest="flt", help="the float tier is built"
    )
    ap.add_argument(
        "--no-falu",
        action="store_true",
        help="the elementwise float group is NOT built",
    )
    ap.add_argument(
        "--no-fsfu", action="store_true", help="the four seeds are NOT built"
    )
    ap.add_argument(
        "--no-facc", action="store_true", help="the float accumulator is NOT built"
    )
    # 0 IS NOT BUILT, and the maximum is SIMD -- there is no 2*SIMD width.
    ap.add_argument(
        "--flanes",
        type=int,
        default=None,
        help="float units built; 0 = none, default SIMD",
    )
    ap.add_argument("--out", default=str(ROOT / "tests" / "pe" / "build" / "khd"))
    a = ap.parse_args()
    if a.flanes is None:
        a.flanes = a.simd
    # `--float --flanes 0` is a contradiction: khs_unit refuses it at
    # elaboration, and the model would divide by zero placing an element.
    if a.flt and a.flanes == 0:
        ap.error(
            "--float with --flanes 0: 0 means the tier is NOT built. "
            "Use --flanes %d for the widest float tier." % a.simd
        )
    # The register-file and accumulator counts are BEHAVIOURAL -- a program
    # written for eight vector registers faults on a build with four -- so they
    # move the generated stream, not just the synthesis.
    global VREGS, NACC
    VREGS, NACC = a.vregs, a.nacc
    # The field table's own range check is what refuses `v31` on a build with
    # eight, so it has to be told too -- otherwise a 32-register configuration
    # cannot even be encoded.
    I.VREGS, I.NACC = a.vregs, a.nacc
    # And the slot count, for `vextr`'s lane index -- the field table refuses one
    # at or above it, exactly as khs_unit does.
    I.SIMD = a.simd
    feat = {
        "ilanes": a.ilanes,
        "shiftu": a.shiftu,
        "permu": a.permu,
        "red": a.red,
        "fcvtu": a.fcvtu,
        "float": a.flt,
        "falu": not a.no_falu,
        "fsfu": not a.no_fsfu,
        "facc": not a.no_facc,
    }
    # The directory names the CONFIGURATION, so a bench cannot read vectors
    # built for a build it is not.
    tag = "s%d_il%d_sh%d_pm%d_r%d_v%d_a%d%s" % (
        a.simd,
        feat["ilanes"],
        feat["shiftu"],
        feat["permu"],
        feat["red"],
        VREGS,
        NACC,
        "_float" if feat["float"] else "",
    )
    build(a.simd, a.out, feat, tag, flanes=a.flanes)
    print("  configuration tag: %s" % tag)


if __name__ == "__main__":
    main()
