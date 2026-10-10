"""Functional models of KohakuTPU's units, and a device that runs on them.

Levels 1 and 0: this decodes the project's own ISA and computes what a unit
would compute. It owns the DATAPATH only -- `kohakuaccel.sim.SimMachine` owns
staging, kick, dispatch and completion -- so what runs here is the same artifact
the driver dispatches to the card, byte for byte.

Not cycle-accurate: no routing, no backpressure, no DSP pipeline. What it does
model is what changes answers -- where every operand comes from, MXFP7
quantisation, the pumped accumulator's pair merge and M14 rounding, E8M15
rounding in the vector lane, and every conversion's saturation to fp16.
"""

import math
from dataclasses import dataclass

import numpy as np
from kohakuaccel.device import (
    SIG_DATA_RECEIVED,
    SIG_INST_COMPLETE,
    encode_caps,
    node_index,
)
from kohakuaccel.machinespec import MachineSpec
from kohakuaccel.memory import Arena
from kohakuaccel.rt import Runtime
from kohakuaccel.sim import MEM_BASE, Memory, Signal, SimMachine, UnitModel
from kohakutpu.hw import mxfp7
from kohakutpu.hw import tensor as T
from kohakutpu.hw import vector as V
from kohakutpu.isa import ISA
from kohakutpu.isa.vecemit import BATCH_BYTES
from kohakutpu.isa.vector import ISA as VEC_ISA
from kohakutpu.rt import FP16, XFORM_MXFP7, Holder
from kohakutpu.units import MATMUL_CODE, VECTOR_CODE

PAYLOAD = (1 << 256) - 1

#: The mesh-wide build number the RTL endpoints report (noc_cu_base CU_VERSION).
CU_VERSION = 6

LANES = 4
KBLOCK = 32
WORD_BYTES = 32

#: L1 is two banks of 256 entries a side, not one flat 512 (isa/cluster.md §4.6).
BANKS = 2
BANK_ENTRIES = 256

FP16_MAX = 65504.0

#: The pumped cluster's accumulator is S1E7M14 (`mx_acu_fp_pump` ACC_MW 14):
#: fourteen stored mantissa bits plus the implicit one.
ACC_MW = 14
ACC_SIG = ACC_MW + 1
#: The pair merge's magnitude width: a K-block's 22-bit partial times the 8-bit
#: scale-mantissa product; an alignment shift is capped there.
ACC_VWM = 30

#: Granules a cluster gathers into one CU_DATA descriptor, so a node-addressed
#: DRAIN of `n` sub-tiles is that many bursts and that many acknowledgements.
WBURST = 8

#: The 6+4 mesh `driver/examples/enumerate_card.py` reads off the silicon.
MG_COORDS = ((1, 1), (1, 2), (1, 3), (2, 1), (2, 2), (2, 3))
VC_COORDS = ((1, 0), (1, 4), (2, 0), (2, 4))

#: Inside `SimMachine`'s 64 MiB window, clear of its base.
ARENA_BASE = MEM_BASE + 0x0010_0000
ARENA_SIZE = 1 << 24


#: `dflags` bit 0 is `signal_on_complete` (isa/cluster.md §9.2).
DFLAG_SIGNAL = 1


class ModelError(RuntimeError):
    """An instruction this model cannot execute, and why."""


#: Where a cluster instruction's address splits, from `isa/cluster.py`.
CU_ADDR_BITS = ISA.cfg.addr_bits


def full_addr(f: dict) -> int:
    """A decoded instruction's 40-bit address, rejoined from its two fields.

    Both encodings SPLIT an address so that widening to 40 bits moved no other
    field. Reading the low part alone drops the aperture bit and the mesh id, so
    a staging or a remote address decodes as local DRAM at the same offset --
    which is a legal read of the wrong window, not a fault.
    """
    return int(f["addr"]) | (int(f.get("addr_hi", 0)) << CU_ADDR_BITS)


@dataclass
class Ack(Signal):
    """A completion owed by the RECEIVER of a transfer, not by its sender."""

    at: tuple = (0, 0)


class Mesh(SimMachine):
    """A `SimMachine` whose units can answer for one another.

    A node-addressed DRAIN is acknowledged by the core it delivered to, so that
    completion has to land in THAT node's mirror: the cluster stage's artifact
    carries an await on a node it never kicked, and a count posted against the
    sender would leave that await unsatisfied forever.
    """

    def _signal(self, idx: int, sig: Signal) -> None:
        at = getattr(sig, "at", None)
        super()._signal(node_index(*at) if at else idx, sig)


def to_acc(x):
    """Round `x` to the accumulator's significand, nearest even. Returns a
    float64 array."""
    m, e = np.frexp(np.asarray(x, np.float64))
    s = float(1 << ACC_SIG)
    return np.ldexp(np.round(m * s) / s, e)


def sweep(a, b):
    """One GEMM sweep: `a @ b.T` the way the cluster computes it.

    `a` is `(rows, k)` and `b` is `(cols, k)`, both fp16 as memory holds them.
    Each is quantised to MXFP7 per K-block; see :func:`sweep_q` for the rest.

    Returns a `(rows, cols)` float64 array.
    """
    return sweep_q(mxfp7.quantise_fp16(a), mxfp7.quantise_fp16(b))


def _block(ia, ib, esa, esb, m8a, m8b, at):
    """One K-block's exact product: sign, integer magnitude, exponent (value =
    magnitude * 2^exponent), the scale mantissas multiplied in as integers."""
    dot = ia[:, at, :] @ ib[:, at, :].T
    mag = np.abs(dot) * (m8a[:, at, None] * m8b[None, :, at])
    exp = esa[:, at, None] + esb[None, :, at] - 6
    return np.sign(dot), mag.astype(np.int64), exp


def sweep_q(qa_, qb_, acc=None):
    """:func:`sweep` on operands already quantised, ``(q, es, m8)`` each, as
    `mx_acu_fp_pump` computes it: K-blocks two at a time (a lone last block
    alone), the pair merged at full width with the smaller-exponent product
    aligned by a TRUNCATING shift (capped at `ACC_VWM`; a carry out drops the
    low bit), rounded to the accumulator nearest even, then added into the tile
    nearest even. `acc` is the tile to add into, or None to open it.

    MEASURED: equal to the Verilated card's cluster on every element of a
    128^3 matmul; exact per-block accumulation at M16 differed on 6%.
    """
    qa, esa, m8a = qa_
    qb, esb, m8b = qb_
    rows, k = np.shape(qa)
    cols = np.shape(qb)[0]
    esa, m8a = np.asarray(esa, np.int64), np.asarray(m8a, np.int64)
    esb, m8b = np.asarray(esb, np.int64), np.asarray(m8b, np.int64)
    blocks = k // KBLOCK
    ia = np.asarray(qa, np.int64).reshape(rows, blocks, KBLOCK)
    ib = np.asarray(qb, np.int64).reshape(cols, blocks, KBLOCK)

    out = acc
    for p in range(0, blocks, 2):
        s0, m0, e0 = _block(ia, ib, esa, esb, m8a, m8b, p)
        if p + 1 < blocks:
            s1, m1, e1 = _block(ia, ib, esa, esb, m8a, m8b, p + 1)
            big1 = e1 > e0
            sb, mb, eb = (
                np.where(big1, s1, s0),
                np.where(big1, m1, m0),
                np.maximum(e0, e1),
            )
            ss, ms = np.where(big1, s0, s1), np.where(big1, m0, m1)
            ms = ms >> np.minimum(np.abs(e0 - e1), ACC_VWM)
            val = sb * mb + ss * ms
            mag = np.abs(val)
            over = mag >= (1 << ACC_VWM)
            mag = np.where(over, mag >> 1, mag)
            eb = np.where(over, eb + 1, eb)
            merged = (
                np.sign(val) * mag.astype(np.float64) * np.exp2(eb.astype(np.float64))
            )
        else:
            merged = (s0 * m0).astype(np.float64) * np.exp2(e0.astype(np.float64))
        t = to_acc(merged)
        out = t if out is None else to_acc(out + t)
    return out


class ClusterUnit(UnitModel):
    """A matmul cluster: two L1 sides, the MXFP7 sweep, and a saturating drain.

    `saturated` counts result elements the drain clamped to the fp16 maximum and
    `clamped` records where, as `(address, element)` pairs -- which is the
    reading this model exists to produce.
    """

    def __init__(
        self,
        version: int = CU_VERSION,
        mem_base: int = MEM_BASE,
        banking: bool = True,
    ) -> None:
        self.caps = encode_caps(MATMUL_CODE, version=version, buffers=BANKS)
        self.mem_base = mem_base
        self.banking = banking
        #: The L1, ``(q, es, m8)`` per side: a FILL reads 128 B MXFP7 entries.
        self.l1q = [
            (
                np.zeros((BANKS * BANK_ENTRIES, LANES, KBLOCK), np.int64),
                np.zeros((BANKS * BANK_ENTRIES, LANES), np.int64),
                np.full((BANKS * BANK_ENTRIES, LANES), 8, np.int64),
            )
            for _ in range(2)
        ]
        self.acc = None
        self.tile = (0, 0)
        #: The last emitting sweep's result, which a fused DRAIN writes.
        self.emitted = None
        self.saturated = 0
        self.clamped: list = []
        self.counts = {"FILL": 0, "GEMM": 0, "DRAIN": 0}
        #: The entries the sweep in flight is reading, per L1 side, or None.
        self.reading: list = [None, None]
        #: Coordinate -> vector core, for a node-addressed DRAIN. Wired by
        #: :class:`SimDevice`; empty until then, so such a drain fails by name.
        self.peers: dict = {}

    def cost(self, flit: int) -> int:
        """Cycles this instruction holds the cluster.

        The same figures `kohakutpu.cost` prices a STATEMENT at, taken from the
        decoded flit instead -- so the analytic model and a simulated run are
        two independent routes to one number and disagreeing is a defect.
        """
        from kohakutpu.cost import MACS_PER_CLUSTER, flits

        name, f = ISA.set.decode(flit & PAYLOAD)
        if name == "FILL":
            return flits(f["n"] * LANES * KBLOCK)
        if name == "GEMM":
            macs = (f["gm"] * LANES) * (f["nk"] * KBLOCK) * (f["gn"] * LANES)
            return -(-macs // MACS_PER_CLUSTER)
        if name == "DRAIN":
            return flits(f["n"] * LANES * LANES)
        return 1

    def execute(self, flit: int, mem: Memory) -> list[Signal]:
        """Run one instruction and report it retired.

        Returns its own SIG_INST_COMPLETE, which is what the artifact's await
        counts, followed by any acknowledgement a peer owes for a transfer this
        instruction started. Raises :class:`ModelError` on one it cannot run.
        """
        name, f = ISA.set.decode(flit & PAYLOAD)
        self.counts[name] = self.counts.get(name, 0) + 1
        acks: list = []
        match name:
            case "FILL":
                self._fill(f, mem)
            case "GEMM":
                self._gemm(f)
            case "DRAIN":
                acks = self._drain(f, mem)
            case _:
                raise ModelError(f"{name} is not a cluster instruction")
        return [Signal(SIG_INST_COMPLETE), *acks]

    def _fill(self, f: dict, mem: Memory) -> None:
        """Stream `n` entries from memory into one L1 side.

        Raises :class:`ModelError` when the entries overlap those the sweep in
        flight is still reading, which is the one L1 hazard a serial reading of
        the program cannot otherwise see.
        """
        n = f["n"]
        at = f["fbank"] * BANK_ENTRIES + f["eoff"]
        if at + n > BANKS * BANK_ENTRIES:
            raise ModelError(
                f"FILL of {n} entries at {at} runs past the "
                f"{BANKS * BANK_ENTRIES}-entry L1 side {f['sel']}"
            )
        self._hazard(f["sel"], at, n)
        where = full_addr(f)
        raw = mem.read(where - self.mem_base, n * T.MXFP7_ENTRY_BYTES)
        if len(raw) < n * T.MXFP7_ENTRY_BYTES:
            raise ModelError(f"FILL at {where:#x} reads past the memory window")
        q, es, m8 = T.from_mxfp7_entries(raw, blayout=f["sel"])
        for held, got in zip(self.l1q[f["sel"]], (q, es, m8), strict=True):
            held[at : at + n] = got

    def _hazard(self, sel: int, at: int, n: int) -> None:
        """Refuse a FILL that lands inside the range a live sweep is reading.

        A GEMM retires when its sweep STARTS, so the unit runs the next FILL
        while the array is still reading L1 and nothing interlocks them. Serial
        execution cannot see it -- the fill would simply be read back -- so the
        program is checked rather than the values.
        """
        live = self.reading[sel]
        if not self.banking or live is None:
            return
        base, span = live
        if at < base + span and base < at + n:
            raise ModelError(
                f"a FILL of {n} entries at {at} lands inside L1 side {sel} "
                f"[{base}, {base + span}), which the sweep in flight is still "
                f"reading. A GEMM retires when its sweep starts, so this "
                f"corrupts sub-tiles silently -- alternate the bank per K chunk"
            )

    def _operand_q(self, sel: int, bank: int, off: int, groups: int, blocks: int):
        """``(q, es, m8)`` of the `groups*LANES x blocks*KBLOCK` tile a sweep reads."""
        at = bank * BANK_ENTRIES + off
        q, es, m8 = (held[at : at + groups * blocks] for held in self.l1q[sel])
        q = q.reshape(groups, blocks, LANES, KBLOCK).transpose(0, 2, 1, 3)
        es = es.reshape(groups, blocks, LANES).transpose(0, 2, 1)
        m8 = m8.reshape(groups, blocks, LANES).transpose(0, 2, 1)
        rows = groups * LANES
        return (
            q.reshape(rows, blocks * KBLOCK),
            es.reshape(rows, blocks),
            m8.reshape(rows, blocks),
        )

    def _gemm(self, f: dict) -> None:
        """Sweep `gm x gn` sub-tiles over `nk` K-blocks into the accumulator."""
        gm, gn, nk = f["gm"], f["gn"], f["nk"]
        into = None
        if f["acc"] and self.acc is not None:
            if self.acc.shape != (gm * LANES, gn * LANES):
                raise ModelError(
                    f"GEMM chains a {(gm * LANES, gn * LANES)} tile onto a "
                    f"{self.acc.shape} one"
                )
            into = self.acc
        got = sweep_q(
            self._operand_q(0, f["abank"], f["aoff"], gm, nk),
            self._operand_q(1, f["bbank"], f["boff"], gn, nk),
            into,
        )
        self.acc = got
        self.tile = (gm, gn)
        self.reading[0] = (f["abank"] * BANK_ENTRIES + f["aoff"], gm * nk)
        self.reading[1] = (f["bbank"] * BANK_ENTRIES + f["boff"], gn * nk)
        # An emitting sweep hands its sub-tiles out as it finishes them; a fused
        # DRAIN only waits for them, however much later it comes.
        if f["emit"]:
            self.emitted = (got, (gm, gn))

    def _drain(self, f: dict, mem: Memory) -> list:
        """Write `n` sub-tiles of the accumulator out as saturating fp16.

        Returns the acknowledgements a node-addressed drain owes; a drain to
        memory owes none. The receiver answers, not the sender, so those land in
        the RECEIVER's mirror -- see :class:`Ack`.
        """
        out = self._subtiles(f)
        # A DRAIN ends the sweep in front of it, so nothing is being read now.
        self.reading = [None, None]
        where = full_addr(f)
        if not f["dnode"]:
            mem.write(where - self.mem_base, out.tobytes())
            return []
        dst = (f["dst_x"], f["dst_y"])
        core = self.peers.get(dst)
        if core is None:
            raise ModelError(
                f"a node-addressed DRAIN names ({dst[0]},{dst[1]}), where this "
                f"machine has no vector core to receive the tile"
            )
        core.receive(where // WORD_BYTES, out.tobytes())
        if not f["dflags"] & DFLAG_SIGNAL:
            return []
        bursts = -(-f["n"] // WBURST)
        return [Ack(SIG_DATA_RECEIVED, arg=f["dbuf"], at=dst)] * bursts

    def _subtiles(self, f: dict):
        """`n` sub-tiles of the accumulator as fp16, in the order a drain emits.

        Raises :class:`ModelError` when no GEMM has filled the accumulator.
        """
        if self.acc is None:
            raise ModelError("DRAIN before any GEMM filled the accumulator")
        acc, (gm, gn) = self.acc, self.tile
        if f["fuse"] and self.emitted is not None:
            acc, (gm, gn) = self.emitted
            self.emitted = None
        held = acc.reshape(gm, LANES, gn, LANES).transpose(0, 2, 1, 3)
        held = held.reshape(gm * gn, LANES * LANES)[: f["n"]]
        over = np.abs(held) > FP16_MAX
        if over.any():
            self.saturated += int(over.sum())
            for t, e in zip(*np.nonzero(over)):
                self.clamped.append((f["addr"] + int(t) * WORD_BYTES, int(e)))
        return np.clip(held, -FP16_MAX, FP16_MAX).astype(FP16)


# ---------------------------------------------------------- the vector lane
#: E8M15's stored mantissa plus its implicit leading one.
LANE_SIG = 16
#: The lane carries FP32's exponent field and has NO subnormals, so it overflows
#: where FP32 does and flushes anything below FP32's smallest normal.
LANE_OVER = 2.0**128
LANE_MIN = 2.0**-126

#: The vector core's 16 slices. `LANES` above is the cluster's sub-tile edge.
SLICES = V.LANES
VLMAX = V.VLMAX
NREG = 16
NDESC = 8
NDIM = 4
L1_DEPTH = 512
IMEM_DEPTH = 512

#: A VFILL or VDRAIN longer than this faults F_LEN, and a kernel that has not
#: halted by `MAX_STEPS` is looping, which on the card is a hang.
MAX_WALK = 256
MAX_STEPS = 1 << 20

#: Where a descriptor base's high field starts, from `isa/vector.py`.
DESC_VALUE_BITS = VEC_ISA.cfg.desc_value_bits

#: `vec_core.v` seeds 0.0, 1.0 and -1.0, and leaves K3 for VSETI.
KREG_SEED = (0x000000, 0x3F8000, 0xBF8000, 0x000000)

SRC_S, SRC_C = V.SRC_S, V.SRC_C
DT_FP16, DT_FP32 = V.DT_FP16, V.DT_FP32
OPNAME = {code: name for name, code in V.OPS.items()}

#: `vec_core.v`'s fault codes, keyed by the name `V.FAULTS` prints for each.
FAULT = {msg.split(":")[0]: code for code, msg in V.FAULTS.items()}

#: VRED kinds taking one element per leaf, so a chunk is two beats not one.
RED_HALF = (3, 4, 5)
#: The ALU op each VRED kind's tree nodes and accumulators run (`vec_lanes.v`
#: `comb_op`), and the word its accumulators are seeded to (`ident`).
RED_COMB = {0: "VADD", 1: "VMAX", 2: "VMIN", 3: "VADD", 4: "VADD", 5: "VADD"}
RED_IDENT = {0: 0x000000, 1: 0xFF8000, 2: 0x7F8000, 3: 0x000000, 4: 0x000000, 5: 0}

#: The three that write a predicate register instead of a vector one.
LANE_PRED = ("VCMPLT", "VCMPGT", "VCMPEQ")

#: `vec_alu.v`'s opcode field. `vec_core` sends the four seeds' ISA opcodes
#: (0x0E..0x11) to the ALU two higher and every other ALU opcode unchanged.
ALU_OP = {
    name: code + (2 if V.OPS["VEXP2"] <= code <= V.OPS["VRSQRT"] else 0)
    for name, code in V.OPS.items()
    if code <= V.OPS["VRSQRT"]
}
SEEDS = ("VEXP2", "VLOG2", "VINV", "VRSQRT")

E8_ONE = 0x3F8000
E8_NAN = 0x7FC000
E8_INF = 0x7F8000
E8_SIGN = 0x800000
E8_MAG = 0x7FFFFF

# ------------------------------------------------- the seeds' coefficient ROM
#: `vec_tables.v`, regenerated by `scripts/py/vec_tables.py`'s fit: c0, c1, c2
#: 22-bit signed at weights 2^-20, 2^-24, 2^-28; rsqrt has an octave-parity bit.
SEED_SEG = 32
SEED_CW = 22
SEED_Q = (20, 24, 28)
SEED_SH = 16


def _seed_fn(fsel: int, idx6: int):
    """Segment `idx6` of seed `fsel` (0 exp2, 1 log2, 2 inv, 3 rsqrt), u in [0,1)."""
    idx, par = idx6 % SEED_SEG, idx6 // SEED_SEG
    if fsel == 0:
        return lambda u: 2.0 ** (idx / 32.0 + u / 32.0) - 1.0
    if fsel == 1:
        return lambda u: math.log2(1.0 + (idx + u) / 32.0)
    if fsel == 2:
        return lambda u: 1.0 / (1.0 + (idx + u) / 32.0)
    scale = 2.0 if par else 1.0
    return lambda u: 1.0 / math.sqrt(scale * (1.0 + (idx + u) / 32.0))


def _seed_fit(g) -> tuple:
    """`g` interpolated at [0,1]'s three Chebyshev nodes: ``(c0, c1, c2)``.

    Gaussian elimination with partial pivoting, in the generator's operation
    order: the quantised coefficients are a function of every rounding here.
    """
    nodes = [0.5 + 0.5 * math.cos((2 * j + 1) * math.pi / 6.0) for j in range(3)]
    rows = [[1.0, x, x * x, g(x)] for x in nodes]
    for col in range(3):
        piv = max(range(col, 3), key=lambda r: abs(rows[r][col]))
        rows[col], rows[piv] = rows[piv], rows[col]
        for r in range(3):
            if r == col:
                continue
            f = rows[r][col] / rows[col][col]
            for k in range(col, 4):
                rows[r][k] -= f * rows[col][k]
    return tuple(rows[i][3] / rows[i][i] for i in range(3))


def seed_table() -> np.ndarray:
    """The ROM as ``[fsel, idx6] -> (c0, c1, c2)``, int64; unused rows are 0."""
    tab = np.zeros((4, 2 * SEED_SEG, 3), np.int64)
    for fsel in range(4):
        for idx6 in range(2 * SEED_SEG if fsel == 3 else SEED_SEG):
            fit = _seed_fit(_seed_fn(fsel, idx6))
            tab[fsel, idx6] = [round(c * (1 << q)) for c, q in zip(fit, SEED_Q)]
    return tab


SEED_TABLE = seed_table()


class VecFault(ModelError):
    """A fault `vec_core` would report, carrying the code it reports."""

    def __init__(self, code: int, detail: str = "") -> None:
        self.code = code
        super().__init__(f"{V.FAULTS[code]}{detail}")


def to_e8m15(x):
    """Round `x` to the vector lane's format. Returns a float64 array.

    Overflow becomes an infinity and anything under the smallest normal becomes
    a zero, because `vec_alu.v` bounds the exponent at both ends.
    """
    m, e = np.frexp(np.asarray(x, np.float64))
    s = float(1 << LANE_SIG)
    out = np.ldexp(np.round(m * s) / s, e)
    mag = np.abs(out)
    out = np.where(mag >= LANE_OVER, np.copysign(np.inf, out), out)
    return np.where(mag < LANE_MIN, np.copysign(0.0, out), out)


def e8_value(bits):
    """24-bit E8M15 words as float64, exactly. E == 0 is a zero whatever M holds."""
    bits = np.asarray(bits, np.int64)
    bits = np.where((bits >> 15) & 0xFF == 0, bits & E8_SIGN, bits & 0xFFFFFF)
    return (bits << 8).astype(np.uint32).view(np.float32).astype(np.float64)


def e8_bits(x):
    """Float64s that are E8M15 values as their 24-bit words, exactly."""
    raw = np.asarray(x, np.float64).astype(np.float32).view(np.uint32)
    return raw.astype(np.int64) >> 8


def e8_of_f32(raw):
    """`vec_cvt_f32_to_e8`: FP32 words to E8M15, RNE, subnormals flushed."""
    raw = np.asarray(raw, np.int64) & 0xFFFFFFFF
    s, e, m = (raw >> 31) << 23, (raw >> 23) & 0xFF, raw & 0x7FFFFF
    keep = m >> 8
    rnd = keep + (((m >> 7) & 1) & (((m & 0x7F) != 0) | (keep & 1)))
    e_adj = e + (rnd >> 15)
    out = np.where(e_adj >= 255, s | E8_INF, s | (e_adj << 15) | (rnd & 0x7FFF))
    out = np.where(e == 255, s | E8_INF | np.where(m != 0, 0x4000 | keep, 0), out)
    return np.where(e == 0, s, out)


def from_bits(raw) -> float:
    """One 24-bit scalar or constant register as a float."""
    return float(e8_value(int(raw)))


def to_bits(x) -> int:
    """A float as a scalar register's 24-bit word, which is FP32's top 24 bits."""
    return int(e8_bits(to_e8m15(x)))


def _wrap(x, bits: int):
    """`x` as a `bits`-wide two's-complement number."""
    half = 1 << (bits - 1)
    return ((x + half) & ((1 << bits) - 1)) - half


def _fields(w):
    """A word's sign, exponent and mantissa."""
    return w >> 23, (w >> 15) & 0xFF, w & 0x7FFF


def _fma(va, vb, vc):
    """The FMA family's specials, magnitude and exponent base (`vec_alu.v`).

    DSP-E aligns the addend with ONE right shift of ``{sig_c, 32'b0}``; a
    negative shift (the product below half the addend's ulp) drops the product
    and passes the addend. The sticky mask is 16 bits wide, so a ZERO addend
    (shift 48) sets sticky from its implicit one: every MUL rounds a tie up.
    """
    sa, ea, ma = _fields(va)
    sb, eb, mb = _fields(vb)
    sc, ec, mc = _fields(vc)
    pz, cz = (ea == 0) | (eb == 0), ec == 0
    sab = sa ^ sb
    neg = sab ^ sc
    an, ai = (ea == 255) & (ma != 0), (ea == 255) & (ma == 0)
    bn, bi = (eb == 255) & (mb != 0), (eb == 255) & (mb == 0)
    cn, ci = (ec == 255) & (mc != 0), (ec == 255) & (mc == 0)
    p_nan = an | bn | (ai & (eb == 0)) | ((ea == 0) & bi)
    p_inf = (ai | bi) & ~p_nan
    f_nan = p_nan | cn | (p_inf & ci & (sab != sc))
    spec = (f_nan, (p_inf | ci) & ~f_nan, np.zeros_like(f_nan))
    sum_e = np.where(pz, 2, ea + eb)
    s_raw = sum_e - ec - 110
    byp = (s_raw < 0) & ~cz
    s_amt = np.where(cz, 48, np.where(byp, 0, np.minimum(s_raw, 48)))
    ebase = np.where(byp, ec - 47, sum_e - 157)
    gc = 0x8000 | mc
    algn = (gc << 32) >> s_amt
    lost = np.clip(s_amt - 32, 0, 16)
    stk = (s_amt >= 33) & ((gc & ((1 << lost) - 1)) != 0)
    prod = np.where(byp | pz, 0, (0x8000 | ma) * (0x8000 | mb))
    p = np.where(neg == 1, algn - prod, algn + prod) & ((1 << 48) - 1)
    res_neg = (neg == 1) & (s_amt != 0) & (p >> 47 == 1)
    mag = np.where(res_neg, -p & ((1 << 33) - 1), p)
    sign = np.where(neg == 1, np.where(res_neg, sab, sc), sab)
    canc = (neg == 1) & ~pz & ~cz
    return spec, np.where(p_inf, sab, sc), mag, sign, stk, ebase, canc


def _seed(name: str, a):
    """A seed's specials, magnitude and exponent base (`vec_alu.v`).

    exp2 reduces x to s8.17 fixed point, R = round-half-up(|x| * 2^17) negated
    for x < 0, in a 25-bit signed word: |x| in [128, 256) is NOT a special
    (only e > 134 is) and wraps, so exp2(200) is 2^-56 and exp2(-200) is 2^56.
    A segment origin (index and u both zero) takes the exact identity value in
    place of c0; h is still evaluated.
    """
    s, e, m = _fields(a)
    z, x = e == 0, e == 255
    n, i = x & (m != 0), x & (m == 0)
    fsel = SEEDS.index(name)
    false = np.zeros_like(z)
    if fsel == 0:
        sh = np.where(e > 134, 0, np.minimum((134 - e) & 0xFF, 26))
        rr = ((1 << 25) | (m << 10)) >> sh
        hi, lo = rr >> 1, rr & 1
        rr_v = _wrap(np.where(s == 1, -(hi + lo), hi + lo), 25)
        k, f = rr_v >> 17, rr_v & 0x1FFFF
        idx, u = f >> 12, f & 0xFFF
        big = i | (e > 134)
        spec = (n, ~n & (s == 0) & big, ~n & (s == 1) & big)
        ssign, ebase = false.astype(np.int64), k + 107
    else:
        idx, u = m >> 10, (m & 0x3FF) << 2
        neg = (s == 1) & ~z
        if fsel == 1:
            spec, ssign, ebase = (n | neg, ~n & ~neg & (i | z), false), z, 107
        elif fsel == 2:
            spec, ssign, ebase = (n, ~n & z, ~n & i), s, 234 - e
        else:
            idx = idx | ((~e & 1) << 5)
            spec = (n | neg, ~n & ~neg & z, ~n & ~neg & i)
            ssign, ebase = (s == 1) & z, 107 - ((e - 127) >> 1)
    ident = (idx & 31 == 0) & (u == 0) & ((fsel != 3) | (idx >> 5 == 0))
    c0, c1, c2 = np.moveaxis(SEED_TABLE[fsel][idx], -1, 0)
    c0 = np.where(ident, 0 if fsel < 2 else 1 << 20, c0)
    c0 = c0 + (1 << 20 if fsel == 0 else ((e - 127) << 20) if fsel == 1 else 0)
    c0 = _wrap(c0, 30)
    h = _wrap((c2 * u + (c1 << SEED_SH) + (1 << 15)) >> SEED_SH, 22)
    f = _wrap((h * u + (c0 << SEED_SH) + (1 << 15)) >> SEED_SH, 30)
    sign = (f < 0) if fsel == 1 else ssign
    return spec, ssign, np.abs(f), sign, false, ebase, false


def lane(name: str, a, b, c):
    """One `vec_alu.v` instruction over 24-bit words: ``(result, predicate)``.

    Every opcode goes through the FMA or a seed and one normaliser: the
    magnitude's leading one sets the exponent over ``ebase``, the 16 bits under
    it round to nearest even with a guard and a sticky, and the exponent bounds
    flush to a signed zero or saturate to an infinity. MOV/NEG/ABS/MAX/MIN/SEL
    ride as ``winner * 1.0 + 0``, which is why a -0 operand comes out +0.
    """
    a, b, c = np.broadcast_arrays(
        *(np.asarray(w, np.int64) & 0xFFFFFF for w in (a, b, c))
    )
    ma, mb = a & E8_MAG, b & E8_MAG
    sa, sb = a >> 23, b >> 23
    nan = (((a >> 15) & 0xFF == 255) & (a & 0x7FFF != 0)) | (
        ((b >> 15) & 0xFF == 255) & (b & 0x7FFF != 0)
    )
    eq = ((ma == 0) & (mb == 0)) | ((ma == mb) & (sa == sb))
    lt = ~(nan | eq) & np.where(sa != sb, sa == 1, (ma < mb) ^ (sa == 1))
    gt = ~nan & ~eq & ~lt
    pred = {"VCMPLT": lt, "VCMPGT": gt, "VCMPEQ": eq & ~nan}.get(
        name, np.zeros_like(lt)
    )
    va = {
        "VMAX": np.where(lt, b, a),
        "VMIN": np.where(gt, b, a),
        "VNEG": a ^ E8_SIGN,
        "VFNMA": a ^ E8_SIGN,
        "VABS": ma,
        "VSEL": np.where(c & E8_MAG != 0, a, b),
    }.get(name, np.where(pred, E8_ONE, 0) if name in LANE_PRED else a)
    vb = b if name in ("VMUL", "VFMA", "VFNMA") else np.full_like(a, E8_ONE)
    vc = {"VADD": c, "VFMA": c, "VFNMA": c, "VSUB": c ^ E8_SIGN}.get(name, 0 * a)
    if name in SEEDS:
        spec, ssign, mag, sign, stk, ebase, canc = _seed(name, a)
    else:
        spec, ssign, mag, sign, stk, ebase, canc = _fma(va, vb, vc)
    nz = mag != 0
    pos = np.frexp(mag.astype(np.float64))[1] - 1
    nrm = np.where(nz, mag << np.clip(47 - pos, 0, 47), 0)
    sig = nrm >> 32
    up = ((nrm >> 31) & 1 == 1) & ((nrm & ((1 << 31) - 1) != 0) | stk | (sig & 1 == 1))
    sig = sig + up
    carry = sig >> 16
    e_fin = pos + ebase + carry
    sign = sign.astype(np.int64) << 23
    ssign = np.asarray(ssign, np.int64) << 23
    out = np.where(e_fin <= 0, sign, sign | (e_fin << 15) | (sig & 0x7FFF))
    out = np.where(e_fin >= 255, sign | E8_INF, out)
    out = np.where(nz, out, np.where(canc, 0, sign))
    out = np.where(spec[2], ssign, out)
    out = np.where(spec[1], ssign | E8_INF, out)
    return np.where(spec[0], E8_NAN, out), pred


def _fold(words, comb: str):
    """A `vec_lanes` tree over the last axis: adjacent pairs, `a` the left one."""
    while words.shape[-1] > 1:
        words, _ = lane(comb, words[..., 0::2], words[..., 1::2], words[..., 1::2])
    return words[..., 0]


def to_fp16(x):
    """`x` as fp16. Returns the array and how many elements saturated.

    A FINITE overflow clamps to the largest finite fp16 rather than becoming an
    infinity, which is what `vec_cvt_e8_to_f16` and `mx_fpacc_to_fp16` both do.
    """
    x = np.asarray(x, np.float64)
    with np.errstate(over="ignore"):
        out = x.astype(FP16)
    over = np.isinf(out) & np.isfinite(x)
    if over.any():
        out = np.where(over, np.copysign(FP16_MAX, x), out.astype(np.float64))
        out = out.astype(FP16)
    return out, int(over.sum())


class VectorUnit(UnitModel):
    """A vector core: instruction memory, eight descriptors, an L1, 16 lanes.

    Takes the three flit kinds -- load an instruction word, set a descriptor
    field, run -- and a RUN executes the loaded image against memory. Every
    value the lanes produce is rounded to E8M15, which is the precision this
    model exists to report; `saturated` counts elements a store clamped to the
    fp16 maximum.
    """

    def __init__(self, version: int = CU_VERSION, mem_base: int = MEM_BASE) -> None:
        self.caps = encode_caps(VECTOR_CODE, version=version, buffers=1)
        self.mem_base = mem_base
        self.imem = np.zeros(IMEM_DEPTH, np.int64)
        self.dbase = np.zeros(NDESC, np.int64)
        self.dstride = np.zeros((NDESC, NDIM), np.int64)
        self.dbound = np.ones((NDESC, NDIM), np.int64)
        self.l1 = np.zeros((L1_DEPTH, WORD_BYTES), np.uint8)
        self.vreg = np.zeros((NREG, VLMAX))
        self.sreg = np.zeros(NREG, np.int64)
        self.kreg = np.array(KREG_SEED, np.int64)
        self.preg = np.zeros((4, VLMAX), bool)
        self.vl = VLMAX
        self.vmode = V.FLAT
        self.saturated = 0
        self.counts = {"IMEM": 0, "DESC": 0, "RUN": 0}
        #: Instructions the last RUN retired. There is no pipeline here, so this
        #: is the only cost the model can report -- it is not a cycle count.
        self.retired = 0

    @property
    def nchunk(self) -> int:
        """Register chunks a VLD, VST or ALU pass walks: ``ceil(VL / 16)``."""
        return -(-self.vl // SLICES)

    def execute(self, flit: int, mem: Memory) -> list[Signal]:
        """Run one instruction and report it retired.

        Every CU instruction signals, RUN included: `run_kernel` waits on one
        completion per flit. Raises :class:`VecFault` for a fault the core would
        report, and :class:`ModelError` for an instruction this cannot run.
        """
        name, f = VEC_ISA.set.decode(flit & PAYLOAD)
        self.counts[name] += 1
        match name:
            case "IMEM":
                self.imem[f["addr"]] = f["word"]
            case "DESC":
                self._descriptor(f)
            case "RUN":
                with np.errstate(all="ignore"):
                    self._kernel(f["pc"], mem)
            case _:
                raise ModelError(f"{name} is not a vector-core flit")
        return [Signal(SIG_INST_COMPLETE)]

    def receive(self, word: int, blob: bytes) -> None:
        """Place a peer's CU_DATA burst in L1 from word `word`.

        Raises :class:`VecFault` for a burst that runs past L1, which the core
        drops whole rather than wrapping the address around.
        """
        n = len(blob) // WORD_BYTES
        if word < 0 or word + n > L1_DEPTH:
            raise VecFault(
                FAULT["F_CUDATA"], f": {n} words at L1 word {word} of {L1_DEPTH}"
            )
        self.l1[word : word + n] = np.frombuffer(blob, np.uint8).reshape(n, WORD_BYTES)

    # -------------------------------------------------------------- addressing
    def _descriptor(self, f: dict) -> None:
        """One descriptor field: 0 is the base, 1..4 a `(stride, bound)` pair.

        A base is a 40-bit address SPLIT across two encoded fields
        (`isa/vector.py:VecConfig`), and it has to be rejoined here: taking the
        low 34 alone drops the aperture bit and the mesh id, so a staging or a
        remote address decodes as local DRAM at the same offset and the model
        reads a window that is not the one the instruction named.
        """
        ad, fld, value = f["ad"], f["fld"], f["value"]
        if fld == 0:
            self.dbase[ad] = value | (int(f.get("value_hi", 0)) << DESC_VALUE_BITS)
        elif fld <= NDIM:
            stride = value >> 16
            self.dstride[ad][fld - 1] = stride - (1 << 18) if stride >> 17 else stride
            # A bound of zero reads as one, so an unused dimension needs no
            # encoding (`vec_agu.v`).
            self.dbound[ad][fld - 1] = (value & 0xFFFF) or 1

    def _walk(self, ad: int, off: int, n: int):
        """`n` addresses from descriptor `ad`, offset by `off`.

        The walker STOPS at its last index rather than wrapping, so a walk
        longer than the descriptor repeats that final address `n - total` times.
        """
        bound = self.dbound[ad]
        k = np.minimum(np.arange(n), int(bound.prod()) - 1)
        addr = np.full(n, int(self.dbase[ad]) + off, np.int64)
        for d in range(NDIM):
            addr += (k % bound[d]) * self.dstride[ad][d]
            k = k // bound[d]
        return addr

    def _span(self, ad: int):
        """Every address one VFILL or VDRAIN walks. Raises F_LEN past 256."""
        total = int(self.dbound[ad].prod())
        if total > MAX_WALK:
            raise VecFault(FAULT["F_LEN"], f": descriptor {ad} walks {total} entries")
        return self._walk(ad, 0, total)

    # --------------------------------------------------------------- sequencer
    def cost(self, flit: int) -> int:
        """Cycles the flit just executed held this core.

        A RUN costs what its image cost, counted as the image ran; loading an
        instruction word or a descriptor field is one. Same figures
        `kohakutpu.cost` prices a statement at, so the analytic model and a
        simulated run are two routes to one number.
        """
        name, _ = VEC_ISA.set.decode(flit & PAYLOAD)
        return self.spent if name == "RUN" else 1

    def _cycles(self, op: int) -> int:
        """One vector instruction's cycles, by opcode. See `hw.vector.cycles`."""
        return V.cycles(op, self.vl)

    def _kernel(self, start: int, mem: Memory) -> None:
        """Execute the loaded image from `start` until VHALT.

        Raises :class:`ModelError` for a kernel that never halts and
        :class:`VecFault` for anything `vec_core` would fault on.
        """
        pc, steps = start, 0
        looping, top, end, count = False, 0, 0, 0
        self.spent = 0
        while True:
            if pc >= IMEM_DEPTH or steps > MAX_STEPS:
                raise ModelError(
                    f"the kernel started at pc {start} reached pc {pc} after "
                    f"{steps} instructions without a VHALT, which is a hang"
                )
            word = int(self.imem[pc])
            op = (word >> 27) & 0x1F
            pc += 1
            steps += 1
            self.spent += self._cycles(op)
            if op == V.OPS["VHALT"]:
                self.retired = steps
                return
            if op == V.OPS["VSETI"]:
                imm = int(self.imem[pc]) & 0xFFFFFF
                pc += 1
                if (word >> 25) & 3 == V.SRC_K:
                    self.kreg[3] = imm
                else:
                    self.sreg[(word >> 17) & 0xF] = imm
            elif op == V.OPS["VLOOP"]:
                if looping:
                    raise VecFault(FAULT["F_LOOP"])
                looping, count = True, int(self.sreg[(word >> 13) & 0xF])
                top, end = pc, pc + ((word >> 9) & 0xF)
            else:
                self._issue(op, word, mem)
            if looping and pc == end:
                count -= 1
                looping = count > 0
                if looping:
                    pc = top

    def _issue(self, op: int, word: int, mem: Memory) -> None:
        """Execute one instruction that is neither VHALT, VSETI nor VLOOP."""
        name = OPNAME.get(op)
        dt, ad = (word >> 24) & 7, (word >> 21) & 7
        vd, va, vb = (word >> 17) & 0xF, (word >> 13) & 0xF, (word >> 9) & 0xF
        off = (word & 0x3FFF) - (0x4000 if word & 0x2000 else 0)
        match name:
            case "VLD":
                self._load(dt, ad, off, vd)
            case "VST":
                self._store(dt, ad, off, vd)
            case "VCVT":
                self._convert(dt, va, vd)
            case "VSHUF":
                # ONLY here: VLD/VST/VCVT/VBCAST share this decode branch, but
                # for them ir[4:1] is the low end of the descriptor offset, and
                # the core forces pm=0 rather than predicating a load by
                # accident (`vec_core.v`, `ls_pm <= (d_op == O_VSHUF) ? ...`).
                self._shuffle(va, vb, vd, (word >> 3) & 3, (word >> 1) & 3)
            case "VBCAST":
                self._broadcast((word >> 25) & 3, va, vd)
            case "VRED":
                self._reduce(word)
            case "VFILL":
                self._fill(ad, off & (L1_DEPTH - 1), mem)
            case "VDRAIN":
                self._sink(word, ad, off & (L1_DEPTH - 1), mem)
            case "VSETVL":
                self._setvl(va)
            case "VSETMODE":
                self.vmode = va & 3
            case "VBAR":
                pass
            case _ if op <= 0x11:
                self._alu(name, word)
            case _:
                raise VecFault(FAULT["F_OPCODE"], f": opcode {op:#04x}")

    def _setvl(self, sreg: int) -> None:
        """``VL = S[sreg]``. Raises F_VL on zero or past VLMAX."""
        want = int(self.sreg[sreg])
        if want == 0 or want > VLMAX:
            raise VecFault(FAULT["F_VL"], f": VSETVL asked for {want}")
        self.vl = want

    # --------------------------------------------------------------- the lanes
    def _source(self, sel: int, reg: int):
        """One ALU operand: a vector register, or a broadcast scalar or constant.

        Raises F_CHAIN for source C, which only D2, D4 and TREE wire up.
        """
        if sel == SRC_C:
            raise VecFault(FAULT["F_CHAIN"])
        if sel == V.SRC_V:
            return self.vreg[reg][: self.nchunk * SLICES]
        return from_bits(self.sreg[reg] if sel == SRC_S else self.kreg[reg & 3])

    def _alu(self, name: str, word: int) -> None:
        """One arithmetic instruction over the active vector length.

        Raises :class:`ModelError` in any mode but FLAT: `vec_core` gathers a
        whole chain there and only its head may name a vector register.
        """
        if self.vmode != V.FLAT:
            raise ModelError(
                f"{name} issued in VMODE {self.vmode}, where `vec_core` gathers "
                f"{2 if self.vmode == V.D2 else 4} instructions into ONE chain. "
                f"In TREE that is a program left in the wrong mode after a VRED "
                f"and it faults F_VSRC on the card; in D2/D4 it is chaining, "
                f"which this model does not execute"
            )
        a = self._source((word >> 25) & 3, (word >> 13) & 0xF)
        b = self._source((word >> 23) & 3, (word >> 9) & 0xF)
        c = self._source((word >> 21) & 3, (word >> 5) & 0xF)
        span = self.nchunk * SLICES
        keep = np.arange(span) < self.vl
        out, pred = lane(name, e8_bits(a), e8_bits(b), e8_bits(c))
        if name in LANE_PRED:
            got = np.broadcast_to(pred, (span,))
            np.copyto(self.preg[(word >> 3) & 3][:span], got, where=keep)
            return
        pm = (word >> 1) & 3
        if pm:
            held = self.preg[(word >> 3) & 3][:span]
            keep = keep & (held if pm == 1 else ~held)
        got = np.broadcast_to(e8_value(out), (span,))
        np.copyto(self.vreg[(word >> 17) & 0xF][:span], got, where=keep)

    def _reduce(self, word: int) -> None:
        """One VRED: the L1 tree over VL elements, into a scalar register.

        ANY and ALL reduce the PREDICATE file, so they need neither TREE mode
        nor a whole number of chunks and are tested first.
        """
        vd, va, vb = (word >> 17) & 0xF, (word >> 13) & 0xF, (word >> 9) & 0xF
        kind = (word >> 5) & 7
        if kind >= 6:
            bits = self.preg[(word >> 3) & 3][: self.vl]
            self.sreg[vd] = (
                KREG_SEED[1] if (bits.any() if kind == 6 else bits.all()) else 0
            )
            return
        if self.vl % SLICES:
            raise VecFault(FAULT["F_REDVL"], f": VL is {self.vl}")
        if self.vmode != V.TREE:
            raise VecFault(FAULT["F_OPCODE"], ": VRED outside TREE mode")
        n = self.vl // SLICES
        a = e8_bits(self.vreg[va][: self.vl]).reshape(n, SLICES)
        comb = RED_COMB[kind]
        if kind in RED_HALF:
            # The leaf takes ONE element, so a chunk is two beats over the two
            # halves of its slices, and eight leaves feed the seven-node tree.
            other = e8_bits(self.vreg[vb][: self.vl]).reshape(n, SLICES)
            leaves, _ = (
                lane("VEXP2", a, 0, 0) if kind == 5 else lane("VMUL", a, other, 0)
            )
            if kind == 5:
                self.vreg[vb][: self.vl] = e8_value(leaves).reshape(-1)
            totals = _fold(leaves.reshape(2 * n, SLICES // 2), comb)
        else:
            totals = _fold(a, comb)
        # Sixteen rotating partials, beat j into partial j mod 16 as
        # `comb(tree, partial)`; the tail beat folds the partials in the tree.
        acc = np.full(SLICES, RED_IDENT[kind], np.int64)
        for j, total in enumerate(totals):
            acc[j % SLICES] = lane(comb, total, acc[j % SLICES], acc[j % SLICES])[0]
        self.sreg[vd] = int(_fold(acc, comb))

    # ---------------------------------------------------------- L1 and memory
    def _load(self, dt: int, ad: int, off: int, vd: int) -> None:
        """``vd = L1[A[ad] + off]``, converting from `dt`. FP16 in is exact."""
        if dt not in (DT_FP16, DT_FP32):
            raise VecFault(FAULT["F_DTYPE"], f": VLD dtype {dt}")
        n = self.nchunk * (2 if dt == DT_FP32 else 1)
        raw = self.l1[self._walk(ad, off, n) & (L1_DEPTH - 1)]
        if dt == DT_FP16:
            got = raw.reshape(-1).view(FP16)
        else:
            got = raw.reshape(self.nchunk, 2 * WORD_BYTES).view(np.uint32).reshape(-1)
            got = e8_value(e8_of_f32(got))
        self.vreg[vd][: got.size] = got

    def _store(self, dt: int, ad: int, off: int, vs: int) -> None:
        """``L1[A[ad] + off] = vs``, converting to `dt`. FP16 out saturates.

        A whole chunk is written whatever VL is: the store walks words and only
        the ALU has a tail mask.
        """
        if dt not in (DT_FP16, DT_FP32):
            raise VecFault(FAULT["F_DTYPE"], f": VST dtype {dt}")
        held = self.vreg[vs][: self.nchunk * SLICES]
        if dt == DT_FP16:
            out, clamped = to_fp16(held)
            self.saturated += clamped
            raw = out.view(np.uint8).reshape(self.nchunk, WORD_BYTES)
        else:
            raw = held.astype(np.float32).view(np.uint8)
            raw = raw.reshape(2 * self.nchunk, WORD_BYTES)
        self.l1[self._walk(ad, off, len(raw)) & (L1_DEPTH - 1)] = raw

    def _convert(self, dt: int, va: int, vd: int) -> None:
        """``vd = va`` through `dt`. FP32 is the identity; FP16 is a round trip."""
        if dt not in (DT_FP16, DT_FP32):
            raise VecFault(FAULT["F_DTYPE"], f": VCVT dtype {dt}")
        held = self.vreg[va][: self.nchunk * SLICES]
        if dt == DT_FP16:
            out, clamped = to_fp16(held)
            self.saturated += clamped
            held = out.astype(np.float64)
        self.vreg[vd][: held.size] = held

    def _shuffle(self, va: int, vb: int, vd: int, pr: int = 0, pm: int = 0) -> None:
        """``vd[i] = va[(i + S[vb]) % 16]`` within each chunk, PREDICATED.

        `pm` 0 writes every lane, 1 writes where ``P[pr]`` is set and 2 or 3
        where it is clear; a lane not written KEEPS what `vd` held. The
        predicate is indexed by the DESTINATION lane, not by the source lane the
        rotate reads.

        NO VL TAIL MASK, unlike an ALU op: a VSHUF writes whole chunks whatever
        VL is, because it takes the load/store write port. Masking it here would
        disagree with silicon at any VL that is not a multiple of 16.
        """
        k = int(self.sreg[vb]) & 0xF
        span = self.nchunk * SLICES
        got = np.roll(self.vreg[va][:span].reshape(-1, SLICES), -k, axis=1)
        keep = np.ones(span, bool)
        if pm:
            held = self.preg[pr][:span]
            keep = held if pm == 1 else ~held
        np.copyto(self.vreg[vd][:span], got.reshape(-1), where=keep)

    def _broadcast(self, sa: int, va: int, vd: int) -> None:
        """``vd = S[va]`` in every lane, or ``S[vd] = va[0]`` the other way."""
        if sa == SRC_S:
            self.vreg[vd][: self.nchunk * SLICES] = from_bits(self.sreg[va])
        else:
            self.sreg[vd] = to_bits(self.vreg[va][0])

    def _fill(self, ad: int, l1off: int, mem: Memory) -> None:
        """Stream descriptor `ad`'s walk from memory into L1 from word `l1off`."""
        for i, at in enumerate(self._span(ad)):
            raw = mem.read(int(at) - self.mem_base, WORD_BYTES)
            if len(raw) < WORD_BYTES:
                raise ModelError(f"VFILL at {int(at):#x} reads past the memory window")
            self.l1[(l1off + i) % L1_DEPTH] = np.frombuffer(raw, np.uint8)

    def _sink(self, word: int, ad: int, l1off: int, mem: Memory) -> None:
        """Stream L1 from word `l1off` out over descriptor `ad`'s walk."""
        if (word >> 24) & 1:
            raise ModelError(
                "a VDRAIN whose sink is a peer CU is not modelled: nothing in "
                "the compiler emits one, and guessing the burst would produce a "
                "legal-looking transfer into the wrong core"
            )
        for i, at in enumerate(self._span(ad)):
            held = self.l1[(l1off + i) % L1_DEPTH]
            mem.write(int(at) - self.mem_base, held.tobytes())


def _walk(base: int, dims: dict) -> list:
    """Every address a walker visits: dimension 0 outermost (mx_tdesc)."""
    out = [base]
    for at in sorted(dims):
        count, stride = dims[at]
        out = [a + i * stride for a in out for i in range(count)]
    return out


def run_move(writes, mem: Memory, mem_base: int = MEM_BASE) -> None:
    """A memory-mover move from its register writes: COPY, FILL or XFORM slot 1.

    Raises :class:`ModelError` for a mode or a transform this model does not run.
    """
    hdr: dict = {}
    dims: dict = {0: {}, 1: {}}
    staged = None
    imm = 0
    for reg, val in writes:
        if reg == 0x40:
            imm = val & 0xFFFF_FFFF
            continue
        if reg == 0x10:
            sel = val & 1
            hdr[sel] = (
                (val >> 4) & ((1 << 40) - 1),
                (val >> 44) & 7,
                (val >> 47) & 0xF,
                (val >> 55) & 0xF,
            )
            dims[sel] = {}
        elif reg == 0x18:
            stride = (val >> 20) & 0xFFFF_FFFF
            stride -= (stride >> 31) << 32
            staged = (val & 1, (val >> 1) & 7, ((val >> 4) & 0xFFFF, stride))
        elif reg == 0x20 and staged is not None:
            dims[staged[0]][staged[1]] = staged[2]
            staged = None
        elif reg == 0 and val & (1 << 16):
            mode = val & 7
            dst = _walk(hdr[1][0], {k: v for k, v in dims[1].items() if k < hdr[1][1]})
            if mode == 4:
                word = imm.to_bytes(4, "little") * (WORD_BYTES // 4)
                for d in dst:
                    mem.write(d - mem_base, word)
                continue
            src = _walk(hdr[0][0], {k: v for k, v in dims[0].items() if k < hdr[0][1]})
            if mode == 0:
                for s, d in zip(src, dst, strict=False):
                    mem.write(d - mem_base, mem.read(s - mem_base, WORD_BYTES))
            elif mode == 5 and hdr[0][2] == XFORM_MXFP7:
                for e, d in enumerate(dst):
                    raw = b"".join(
                        mem.read(s - mem_base, WORD_BYTES)
                        for s in src[e * 8 : e * 8 + 8]
                    )
                    entry = np.frombuffer(raw, FP16).reshape(LANES, KBLOCK)
                    words = T.to_mxfp7_words_tiled(entry, 1, 1, hdr[0][3] & 1)
                    mem.write(
                        d - mem_base, b"".join(w.to_bytes(32, "little") for w in words)
                    )
            else:
                raise ModelError(
                    f"the mover model runs COPY, FILL and XFORM slot 1, not mode {mode}"
                )


def _is_cluster(unit) -> bool:
    """Whether `unit` is a matmul cluster rather than a vector core."""
    return isinstance(unit, ClusterUnit)


class SimDevice(Holder, Runtime):
    """A KohakuTPU whose units are Python models, with no card attached.

    Takes the same tensors and runs the same kernels as `kohakutpu.rt.Device`,
    through the same artifact, so a result here is comparable with the card's.
    """

    def __init__(
        self,
        mg=MG_COORDS,
        vc=VC_COORDS,
        base: int = ARENA_BASE,
        size: int = ARENA_SIZE,
        agent=(1, 1),
        banking: bool = True,
    ) -> None:
        units = {c: ClusterUnit(banking=banking) for c in mg}
        cores = {c: VectorUnit() for c in vc}
        units.update(cores)
        for unit in units.values():
            if isinstance(unit, ClusterUnit):
                unit.peers = cores
        self.card = Mesh(units=units)
        machine = MachineSpec(
            name="kohakutpu-model",
            units={"MG": tuple(mg), "VC": tuple(vc)},
            inst_depth=512,
            agent=agent,
        )
        super().__init__(
            machine,
            Arena(base, size, align=BATCH_BYTES, mesh=machine.default),
            self.card,
        )

    def host_move(self, writes: list) -> None:
        """A mover move run by the model (:func:`run_move`)."""
        run_move(writes, self.card.mem)

    @property
    def clusters(self) -> list:
        """Every cluster model on this machine, in coordinate order."""
        return [u for _, u in sorted(self.card.units.items()) if _is_cluster(u)]

    @property
    def cores(self) -> list:
        """Every vector-core model on this machine, in coordinate order."""
        return [u for _, u in sorted(self.card.units.items()) if not _is_cluster(u)]

    @property
    def saturated(self) -> int:
        """Elements a cluster drain or a vector store clamped to the fp16 max."""
        return sum(u.saturated for u in self.card.units.values())

    @property
    def clamped(self) -> list:
        """Where the DRAINS clamped, as ``(address, element)`` pairs.

        A vector store clamps into L1 and has no memory address to name, so it
        is counted by :attr:`saturated` and not recorded here.
        """
        return [where for u in self.clusters for where in u.clamped]

    def __repr__(self) -> str:
        return (
            f"SimDevice({len(self.machine.units['MG'])} MG, "
            f"{len(self.machine.units['VC'])} VC, "
            f"{self.arena.used:,}/{self.arena.size:,} bytes used)"
        )
