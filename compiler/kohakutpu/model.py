"""Functional models of KohakuTPU's units, and a device that runs on them.

Levels 1 and 0: this decodes the project's own ISA and computes what a unit
would compute. It owns the DATAPATH only -- `kohakuaccel.sim.SimMachine` owns
staging, kick, dispatch and completion -- so what runs here is the same artifact
the driver dispatches to the card, byte for byte.

Not cycle-accurate and not bit-accurate below the arithmetic: no routing, no
backpressure, no DSP pipeline. What it does model is what changes answers --
where every operand comes from, MXFP7 quantisation, ACC24 accumulation, E8M15
rounding in the vector lane, and every conversion's saturation to fp16.
"""

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
from kohakutpu.hw import vector as V
from kohakutpu.isa import ISA
from kohakutpu.isa.vecemit import BATCH_BYTES
from kohakutpu.isa.vector import ISA as VEC_ISA
from kohakutpu.rt import FP16, Holder
from kohakutpu.units import MATMUL_CODE, VECTOR_CODE

PAYLOAD = (1 << 256) - 1

LANES = 4
KBLOCK = 32
ENTRY_BYTES = LANES * KBLOCK * 2
WORD_BYTES = 32

#: L1 is two banks of 256 entries a side, not one flat 512 (isa/cluster.md §4.6).
BANKS = 2
BANK_ENTRIES = 256

FP16_MAX = 65504.0

#: ACC24 is S1E7M16, so sixteen stored mantissa bits plus the implicit one.
ACC_SIG = 17

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


def to_acc24(x):
    """Round `x` to ACC24's significand. Returns a float64 array."""
    m, e = np.frexp(np.asarray(x, np.float64))
    s = float(1 << ACC_SIG)
    return np.ldexp(np.round(m * s) / s, e)


def sweep(a, b):
    """One GEMM sweep: `a @ b.T` the way the cluster computes it.

    `a` is `(rows, k)` and `b` is `(cols, k)`, both fp16 as memory holds them.
    Each is quantised to MXFP7 per K-block, the significands multiply as
    integers, and the block scales are applied afterwards -- which is what
    `mx_acu_fp.v` does. Blocks accumulate through ACC24, one round per block.

    Returns a `(rows, cols)` float64 array.
    """
    qa, esa, m8a = mxfp7.quantise_fp16(a)
    qb, esb, m8b = mxfp7.quantise_fp16(b)
    rows, k = a.shape
    cols = b.shape[0]
    blocks = k // KBLOCK
    ia = np.asarray(qa, np.int64).reshape(rows, blocks, KBLOCK)
    ib = np.asarray(qb, np.int64).reshape(cols, blocks, KBLOCK)

    out = np.zeros((rows, cols))
    for at in range(blocks):
        dot = (ia[:, at, :] @ ib[:, at, :].T).astype(np.float64)
        scale = 2.0 ** (esa[:, at, None] + esb[None, :, at])
        scale = scale * (m8a[:, at, None] * m8b[None, :, at]) / 64.0
        out = to_acc24(out + dot * scale)
    return out


class ClusterUnit(UnitModel):
    """A matmul cluster: two L1 sides, the MXFP7 sweep, and a saturating drain.

    `saturated` counts result elements the drain clamped to the fp16 maximum and
    `clamped` records where, as `(address, element)` pairs -- which is the
    reading this model exists to produce.
    """

    def __init__(
        self, version: int = 4, mem_base: int = MEM_BASE, banking: bool = True
    ) -> None:
        self.caps = encode_caps(MATMUL_CODE, version=version, buffers=BANKS)
        self.mem_base = mem_base
        self.banking = banking
        self.l1 = [
            np.zeros((BANKS * BANK_ENTRIES, LANES, KBLOCK), FP16) for _ in range(2)
        ]
        self.acc = None
        self.tile = (0, 0)
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
        raw = mem.read(where - self.mem_base, n * ENTRY_BYTES)
        if len(raw) < n * ENTRY_BYTES:
            raise ModelError(f"FILL at {where:#x} reads past the memory window")
        # An entry is `lanes` rows by `kblock` of K, lane-major (isa/cluster.md
        # §3). That order is the machine's, not the packer's.
        got = np.frombuffer(raw, FP16).reshape(n, LANES, KBLOCK)
        self.l1[f["sel"]][at : at + n] = got

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

    def _operand(self, sel: int, bank: int, off: int, groups: int, blocks: int):
        """The `groups*LANES x blocks*KBLOCK` tile a sweep reads from one side."""
        at = bank * BANK_ENTRIES + off
        got = self.l1[sel][at : at + groups * blocks]
        got = got.reshape(groups, blocks, LANES, KBLOCK)
        return got.transpose(0, 2, 1, 3).reshape(groups * LANES, blocks * KBLOCK)

    def _gemm(self, f: dict) -> None:
        """Sweep `gm x gn` sub-tiles over `nk` K-blocks into the accumulator."""
        gm, gn, nk = f["gm"], f["gn"], f["nk"]
        a = self._operand(0, f["abank"], f["aoff"], gm, nk)
        b = self._operand(1, f["bbank"], f["boff"], gn, nk)
        got = sweep(a, b)
        if f["acc"] and self.acc is not None:
            if self.acc.shape != got.shape:
                raise ModelError(
                    f"GEMM chains a {got.shape} tile onto a {self.acc.shape} one"
                )
            got = to_acc24(self.acc + got)
        self.acc = got
        self.tile = (gm, gn)
        self.reading[0] = (f["abank"] * BANK_ENTRIES + f["aoff"], gm * nk)
        self.reading[1] = (f["bbank"] * BANK_ENTRIES + f["boff"], gn * nk)

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
        gm, gn = self.tile
        held = self.acc.reshape(gm, LANES, gn, LANES).transpose(0, 2, 1, 3)
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
#: What each VRED kind folds with, and what its accumulators are seeded to.
RED_COMB = {0: np.add, 1: np.maximum, 2: np.minimum, 3: np.add, 4: np.add, 5: np.add}
RED_IDENT = {0: 0.0, 1: -np.inf, 2: np.inf, 3: 0.0, 4: 0.0, 5: 0.0}

#: Every ALU opcode as ``(a, b, c) -> value``. MOV/NEG/ABS/MAX/MIN/SEL pick an
#: operand and pass it through the FMA as `winner * 1.0 + 0`, which is exact.
LANE_VALUE = {
    "VMOV": lambda a, b, c: a,
    "VNEG": lambda a, b, c: -a,
    "VABS": lambda a, b, c: np.abs(a),
    "VADD": lambda a, b, c: a + c,
    "VSUB": lambda a, b, c: a - c,
    "VMUL": lambda a, b, c: a * b,
    "VFMA": lambda a, b, c: a * b + c,
    "VFNMA": lambda a, b, c: -(a * b) + c,
    "VMAX": lambda a, b, c: np.where(a < b, b, a),
    "VMIN": lambda a, b, c: np.where(a > b, b, a),
    "VSEL": lambda a, b, c: np.where(c != 0.0, a, b),
    "VEXP2": lambda a, b, c: np.exp2(a),
    "VLOG2": lambda a, b, c: np.log2(a),
    "VINV": lambda a, b, c: 1.0 / a,
    "VRSQRT": lambda a, b, c: 1.0 / np.sqrt(a),
}

#: The three that write a predicate register instead of a vector one.
LANE_PRED = {"VCMPLT": np.less, "VCMPGT": np.greater, "VCMPEQ": np.equal}


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


def from_bits(raw) -> float:
    """One 24-bit scalar or constant register as a float."""
    return float(np.array(int(raw) << 8, np.uint32).view(np.float32))


def to_bits(x) -> int:
    """A float as a scalar register's 24-bit word, which is FP32's top 24 bits."""
    return int(np.float32(to_e8m15(x)).view(np.uint32)) >> 8


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


def reduce_tree(values, comb):
    """Fold `values` down its first axis pairwise, as `vec_lanes` wires the tree.

    Every node is one ALU, so every level rounds and the answer depends on the
    tree's shape as much as on the operands.
    """
    cur = np.asarray(values, np.float64)
    while cur.shape[0] > 1:
        cur = to_e8m15(comb(cur[0::2], cur[1::2]))
    return cur[0]


class VectorUnit(UnitModel):
    """A vector core: instruction memory, eight descriptors, an L1, 16 lanes.

    Takes the three flit kinds -- load an instruction word, set a descriptor
    field, run -- and a RUN executes the loaded image against memory. Every
    value the lanes produce is rounded to E8M15, which is the precision this
    model exists to report; `saturated` counts elements a store clamped to the
    fp16 maximum.
    """

    def __init__(self, version: int = 4, mem_base: int = MEM_BASE) -> None:
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
        if name in LANE_PRED:
            got = np.broadcast_to(LANE_PRED[name](a, b), (span,))
            np.copyto(self.preg[(word >> 3) & 3][:span], got, where=keep)
            return
        pm = (word >> 1) & 3
        if pm:
            held = self.preg[(word >> 3) & 3][:span]
            keep = keep & (held if pm == 1 else ~held)
        got = to_e8m15(LANE_VALUE[name](a, b, c))
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
        a = self.vreg[va][: self.vl].reshape(n, SLICES)
        comb, ident = RED_COMB[kind], RED_IDENT[kind]
        if kind in RED_HALF:
            # The leaf takes ONE element, so a chunk is two beats over the two
            # halves of its slices, and eight leaves feed the seven-node tree.
            other = self.vreg[vb][: self.vl].reshape(n, SLICES)
            leaves = to_e8m15(np.exp2(a) if kind == 5 else a * other)
            if kind == 5:
                self.vreg[vb][: self.vl] = leaves.reshape(-1)
            beats = leaves.reshape(2 * n, SLICES // 2).T
        else:
            beats = a.T
        acc = np.full(SLICES, ident)
        totals = reduce_tree(beats, comb)
        acc[: totals.size] = to_e8m15(comb(totals, ident))
        self.sreg[vd] = to_bits(reduce_tree(acc.reshape(SLICES, 1), comb)[0])

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
            got = raw.reshape(self.nchunk, 2 * WORD_BYTES).view(np.float32).reshape(-1)
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
