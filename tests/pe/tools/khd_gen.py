"""Build the DSP datapath's component test: instruction streams and golden state.

    python tests/pe/tools/khd_gen.py --simd 8

Writes tests/pe/build/khd/s<SIMD>/{prog,vfin,afin,spinit,spfin,scal,meta}.hex,
which tests/pe/tb/khd_unit_tb.v walks.

PUSH BUGS DOWN. This drives `khd_unit` on its own, with no core around it, so a
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

from rv_asm import to_hex                                       # noqa: E402
import rv_dsp_isa as I                                          # noqa: E402
from rv_dsp_model import DspMachine, VSPAD_BASE                 # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[3]

VREGS = 8
NACC = 2
VSPAD_ENTRIES = 64          # small: the bench dumps it word for word
MASK32 = 0xFFFF_FFFF

#: Which instructions a configuration carries. A variant is verified AS ITSELF:
#: a stream that used an encoding the build refuses would fault rather than
#: measure, and a stream that avoided one the build HAS would leave it untested.
FEAT_ALL = {"shift": True, "perm": True, "muls": 4, "float": False}

#: Rotating partials per float accumulator. ARCHITECTURAL -- float addition
#: does not associate, so the model must carry the number the RTL was built with.
NPART = 16

#: `vfredsum` is encoded and NOT BUILT: the unit faults on it, so a generator
#: that emitted one would fail the gate for the right reason at the wrong time.
NOT_BUILT = ("vfredsum.f16", "vfredsum.f32")


class Stream:
    """An instruction stream and the model state it produces."""

    def __init__(self, simd, seed=0, feat=None):
        self.m = DspMachine(simd=simd, vregs=VREGS, nacc=NACC,
                            vspad_entries=VSPAD_ENTRIES,
                            imem_words=8, spad_words=8)
        self.simd = simd
        self.vb = simd * 4                      # bytes per vector
        self.rng = random.Random(seed)
        self.prog = []                          # (instr, addr, xdata)
        self.scalars = []
        self.writes = []                        # (index, name, vd, value)
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
            return bool(self.feat.get("float")) and name not in NOT_BUILT
        if op.group == I.F3_VSHI and not self.feat["shift"]:
            return False
        if op.group == I.F3_VPRM and not self.feat["perm"]:
            return False
        # int8 needs four products per lane, for vmul as much as for vdot.
        if name.endswith(".s8") and name.startswith(("vdot", "vdotn", "vmul")) \
                and self.feat["muls"] < 4:
            return False
        return True

    def seed_vspad(self, f16=False):
        # BANDED FP16 FOR THE FLOAT CASES, not uniform words. One FP16 in 32 is
        # an infinity or a NaN, a NaN accumulates to a NaN whatever the hardware
        # does, and after thirty accumulates most slots would be NaN -- so a
        # dropped accumulate or a doubled zero-sweep would still compare equal.
        n = VSPAD_ENTRIES * self.simd
        if f16:
            words = [(self.f16v() << 16) | self.f16v() for _ in range(n)]
        else:
            words = [self.rng.getrandbits(32) for _ in range(n)]
        self.m.load_vspad_words(words)
        return words

    def f16v(self):
        """One finite FP16, exponent banded so 2^-6 <= |x| < 2^6."""
        return ((self.rng.getrandbits(1) << 15)
                | (self.rng.randrange(9, 21) << 10) | self.rng.getrandbits(10))

    def emit(self, name, **o):
        # Clamp every register index to what this build has, once, here --
        # rather than at each of the hundred call sites that would have to
        # remember.
        for k in ("vd", "vs1", "vs2", "vs"):
            if k in o:
                o[k] = self.v(o[k])
        for k in ("ad", "as1"):
            if k in o:
                o[k] = self.a(o[k])
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
        """Zero the accumulators, then fill every vector register.

        The `vaccz` prologue is the same discipline as `rv_gen.zero_regs()` and
        for the same reason: the accumulator array has no reset, so it survives
        between cases while the model starts every case from zero. Resetting 512
        flops in RTL to paper over that would spend control sets on state
        software has to initialise anyway.
        """
        for a in range(NACC):
            self.emit("vaccz", ad=a)
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
        # Accumulators: seed, accumulate, subtract, read back.
        for et in ("s8", "s16"):
            if not self.has("vdot.%s" % et):
                continue
            self.emit("vaccz", ad=0)
            for _ in range(4):
                self.emit("vdot.%s" % et, ad=0, vs1=1, vs2=2)
            self.emit("vdotn.%s" % et, ad=0, vs1=1, vs2=3)
            self.emit("vaccrd", vd=7, as1=0)
            self.emit("vaccwr", ad=1, vs1=7)
            self.emit("vdot.%s" % et, ad=1, vs1=2, vs2=3)
            self.emit("vaccrd", vd=6, as1=1)
        # Moves and reductions.
        self.m.x[6] = 0x1234_5678
        self.emit("vsplat", vd=2, xs1=6)
        for ln in range(min(8, self.simd)):
            self.emit("vextr", xd=7, vs1=1, sh=ln)
        self.emit("vredsum", xd=7, vs1=1)
        self.emit("vredmax", xd=7, vs1=1)
        if self.feat["perm"]:
            # Every slide index, so a lane picked from the wrong side shows.
            for k in range(8):
                self.emit("vsldw%d" % k, vd=3, vs1=1, vs2=2)
            self.emit("vpack.s16", vd=4, vs1=1, vs2=2)
            self.emit("vpack.s32", vd=5, vs1=1, vs2=2)
            for op in ("vunpkl.s8", "vunpkh.s8", "vunpkl.s16", "vunpkh.s16"):
                self.emit(op, vd=6, vs1=1)
        # Stores, then loads back from the same rows.
        for v in range(4):
            self.emit("vst", vs=v, imm=(VSPAD_ENTRIES // 2 + v) * self.vb,
                      xs1=self.base_reg)
        for v in range(4):
            self.emit("vld", vd=v, imm=(VSPAD_ENTRIES // 2 + v) * self.vb,
                      xs1=self.base_reg)

    def saturation(self):
        """Drive the saturating ops onto their bounds, which random data misses."""
        self.load_all()
        for et, pat in (("s8", 0x7F7F7F7F), ("s16", 0x7FFF7FFF), ("s32", 0x7FFFFFFF)):
            self.m.x[6] = pat
            self.emit("vsplat", vd=1, xs1=6)
            self.m.x[6] = 0x01010101 if et == "s8" else 1
            self.emit("vsplat", vd=2, xs1=6)
            self.emit("vsadd.%s" % et, vd=3, vs1=1, vs2=2)   # overflows positive
            self.emit("vadd.%s" % et, vd=4, vs1=1, vs2=2)    # wraps instead
            self.m.x[6] = 0x80808080 if et == "s8" else (
                0x80008000 if et == "s16" else 0x80000000)
            self.emit("vsplat", vd=1, xs1=6)
            self.emit("vssub.%s" % et, vd=5, vs1=1, vs2=2)   # overflows negative
            self.emit("vsub.%s" % et, vd=6, vs1=1, vs2=2)
        # A pack whose sources are outside the narrow range in both directions.
        if self.feat["perm"]:
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
            self.emit("vadd.s32", vd=4, vs1=1, vs2=1)        # distance 1
            self.emit("vsub.s32", vd=5, vs1=4, vs2=1)        # distance 1 again
            self.emit("vmul.s16", vd=2, vs1=5, vs2=4)        # the extra cycle
            self.emit("vadd.s16", vd=3, vs1=2, vs2=2)        # reads it at once
            self.emit("vaccz", ad=0)
            self.emit("vdot.s16", ad=0, vs1=2, vs2=3)
            self.emit("vdot.s16", ad=0, vs1=3, vs2=2)        # back to back, II=1
            self.emit("vaccrd", vd=6, as1=0)                 # drains the pipe
            self.emit("vredsum", xd=7, vs1=6)
            self.emit("vst", vs=6, imm=0, xs1=self.base_reg)
            self.emit("vld", vd=7, imm=0, xs1=self.base_reg)  # store then load

    def float_directed(self):
        """The float accumulator: zero, accumulate, seed, subtract, read back.

        Every read-back is issued with NO GAP behind the last accumulate, and
        every accumulate with no gap behind a zero. Both are the cases the
        hazards exist for: a float accumulate is in flight for fifteen cycles
        after it retires, and a zero sweeps the partials for NPART.
        """
        self.load_all()
        for a in range(NACC):
            self.emit("vfaccz", ad=a)
        # More accumulates than partials, so the rotation WRAPS rather than
        # each partial being written once.
        for i in range(NPART + 12):
            self.emit("vfmacc.f16", ad=0, vs1=1 + (i % 3), vs2=4 + (i % 3))
        self.emit("vfaccrd.f16", vd=0, as1=0)

        self.emit("vfaccwr.f16", ad=1, vs1=2)
        for i in range(NPART // 2):
            self.emit("vfmsac.f16", ad=1, vs1=3, vs2=5)
        self.emit("vfaccrd.f16", vd=1, as1=1)

        # A zero straight after a stream of accumulates, then one accumulate:
        # a sweep that re-armed would land on top of that accumulate's write.
        self.emit("vfaccz", ad=0)
        self.emit("vfmacc.f16", ad=0, vs1=1, vs2=2)
        self.emit("vfaccrd.f16", vd=2, as1=0)

        for a in range(NACC):
            self.emit("vfaccz", ad=a)
            for _ in range(3):
                self.emit("vfmacc.f16", ad=a, vs1=6, vs2=7)
            self.emit("vfaccrd.f16", vd=3 + a, as1=a)

        # Two accumulators interleaved: the turn counter is per accumulator.
        if NACC > 1:
            for a in range(NACC):
                self.emit("vfaccz", ad=a)
            for i in range(NPART + 5):
                self.emit("vfmacc.f16", ad=i % NACC, vs1=1, vs2=2 + (i % 2))
            for a in range(NACC):
                self.emit("vfaccrd.f16", vd=5 + a, as1=a)

    def float_random(self, n):
        """The float tier only, on banded data, with the accumulators read out."""
        self.load_all()
        for a in range(NACC):
            self.emit("vfaccz", ad=a)
        pool = [k for k in I.ISA
                if I.ISA[k].opcode != I.OPC_KHD and self.has(k)]
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
            self.emit("vfaccrd.f16", vd=a, as1=a)

    def random(self, n):
        self.load_all()
        # THE INTEGER TIER ONLY. The float instructions have their own cases,
        # on banded data, for the reason `seed_vspad` gives.
        pool = [k for k in I.ISA if k not in ("vld", "vst") and self.has(k)
                and I.ISA[k].opcode == I.OPC_KHD]
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
                self.emit("vst", vs=self.rng.randrange(VREGS),
                          imm=row * self.vb, xs1=self.base_reg)
                self.emit("vld", vd=self.rng.randrange(VREGS),
                          imm=row * self.vb, xs1=self.base_reg)


#: (name, builder, FP16-banded scratchpad)
INT_CASES = [
    ("directed", lambda s: s.directed(), False),
    ("saturate", lambda s: s.saturation(), False),
    ("hazards", lambda s: s.hazards(), False),
    ("random1", lambda s: s.random(300), False),
    ("random2", lambda s: s.random(300), False),
    ("random3", lambda s: s.random(300), False),
]
F16_CASES = [
    ("floatd", lambda s: s.float_directed(), True),
    ("floatr", lambda s: s.float_random(200), True),
]


def cases_for(feat):
    return INT_CASES + (F16_CASES if feat.get("float") else [])


def build(simd, outdir, feat, tag):
    # One fixed directory, because the bench cannot be handed a string: it
    # reads `cur` and verifies the configuration out of meta instead.
    d = pathlib.Path(outdir) / "cur"
    d.mkdir(parents=True, exist_ok=True)
    cases = cases_for(feat)
    prog, vfin, afin, spinit, spfin, scal, meta = [], [], [], [], [], [], []
    wtr, names = [], []
    for n, (name, fn, f16) in enumerate(cases):
        s = Stream(simd, seed=1000 + n, feat=feat)
        spinit.append(s.seed_vspad(f16))
        fn(s)
        wtr.append(s.writes)
        names.append([w[1] for w in s.writes])
        prog.append(s.prog)
        vfin.append([(s.m.v[v] >> (32 * k)) & MASK32
                     for v in range(VREGS) for k in range(simd)])
        afin.append([s.m.acc[a][k] & MASK32
                     for a in range(NACC) for k in range(simd)])
        spfin.append(s.m.vspad_words())
        scal.append(s.scalars)
        print("  %-9s %5d instructions, %3d scalar results"
              % (name, len(s.prog), len(s.scalars)))

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
        "".join(("%0" + str((ww + 3) // 4) + "x\n") % w for w in flat_w))
    (d / "wcounts.hex").write_text(to_hex([len(w) for w in wtr]))
    (d / "wnames.txt").write_text(
        "".join("%d\t%d\t%s\n" % (c, i, n)
                for c, ns in enumerate(names) for i, n in enumerate(ns)))

    (d / "prog.hex").write_text(
        "".join("%024x\n" % w for w in flat_prog))
    (d / "scal.hex").write_text(to_hex(flat_scal))
    (d / "vfin.hex").write_text(to_hex([w for c in vfin for w in c]))
    (d / "afin.hex").write_text(to_hex([w for c in afin for w in c]))
    (d / "spinit.hex").write_text(to_hex([w for c in spinit for w in c]))
    (d / "spfin.hex").write_text(to_hex([w for c in spfin for w in c]))
    # The vectors CARRY their configuration and the bench checks it against its
    # own parameters. A `-d TAG=name` string would have done the same job, but
    # xvlog gives a bare define an identifier rather than a string literal, and
    # a check the bench can make itself is worth more than a path it is told.
    meta = [len(cases), ni, ns, VREGS, NACC, VSPAD_ENTRIES, simd, nw,
            feat["muls"], 1 if feat["shift"] else 0, 1 if feat["perm"] else 0,
            1 if feat.get("float") else 0]
    (d / "meta.hex").write_text(to_hex(meta))
    (d / "counts.hex").write_text(to_hex([len(p) for p in prog]))
    (d / "scounts.hex").write_text(to_hex([len(x) for x in scal]))
    print("  %d cases, stride %d instructions, into %s" % (len(cases), ni, d))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--simd", type=int, default=8, choices=(2, 4, 8))
    ap.add_argument("--muls", type=int, default=4, choices=(2, 4))
    ap.add_argument("--vregs", type=int, default=8)
    ap.add_argument("--nacc", type=int, default=2)
    ap.add_argument("--no-shift", action="store_true")
    ap.add_argument("--no-perm", action="store_true")
    ap.add_argument("--f16", action="store_true", help="the float tier is built")
    ap.add_argument("--out",
                    default=str(ROOT / "tests" / "pe" / "build" / "khd"))
    a = ap.parse_args()
    # The register-file and accumulator counts are BEHAVIOURAL -- a program
    # written for eight vector registers faults on a build with four -- so they
    # move the generated stream, not just the synthesis.
    global VREGS, NACC
    VREGS, NACC = a.vregs, a.nacc
    # The field table's own range check is what refuses `v31` on a build with
    # eight, so it has to be told too -- otherwise a 32-register configuration
    # cannot even be encoded.
    I.VREGS, I.NACC = a.vregs, a.nacc
    feat = {"shift": not a.no_shift, "perm": not a.no_perm, "muls": a.muls,
            "float": a.f16}
    # The directory names the CONFIGURATION, so a bench cannot read vectors
    # built for a build it is not.
    tag = "s%d_m%d_v%d_a%d%s%s%s" % (a.simd, a.muls, VREGS, NACC,
                                     "" if feat["shift"] else "_nosh",
                                     "" if feat["perm"] else "_nopm",
                                     "_f16" if feat["float"] else "")
    build(a.simd, a.out, feat, tag)
    print("  configuration tag: %s" % tag)


if __name__ == "__main__":
    main()
