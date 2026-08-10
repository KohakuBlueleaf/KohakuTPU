"""How long a level-3 program takes, in cycles.

`mesh.py` answers "what does it compute". This answers "how long does it run",
which is the other half of level 3 and the one a schedule is chosen for.

It is an ANALYTIC model, not a cycle-accurate one. Every instruction costs what
it moves or multiplies, each node runs its own stream, and they run at once --
so the answer is the slowest node. It does NOT model memory contention between
clusters, NoC queueing, or the overlap of a FILL with the GEMM before it.

That last one is why the answer is a bracket, not a number: `cycles` charges
every fill, `overlapped` hides them all, and the machine double-buffers A so it
lands between. The measured 81.2% of peak at 4 CU and 72.7% at 8 CU
(docs/perf.md) both sit inside it -- the strongest claim this model earns.
"""

from dataclasses import dataclass, field

from ktpu.ir.program import Opcode, Program
from ktpu.target import Target

#: MACs a cluster retires per cycle: 4 TCU of 4x8x4, one sub-tile x 32 K.
#: Measured, not nominal -- `hw/sim.py` uses the same figure for its budget.
MACS_PER_CLUSTER = 512

#: Payload bits in one 288-bit flit (isa/cluster.md s2), so a transfer of `b`
#: bits occupies `b / FLIT_BITS` cycles on the link that carries it.
FLIT_BITS = 256

#: Memory widths. ACC24 and E8M15 are internal and never a memory word, so
#: they are deliberately absent (isa/cluster.md s5, compute/vector-core.md).
BITS = {"fp16": 16, "fp32": 32}


@dataclass
class Timing:
    """Cycles for a program, and where they went.

    `cycles` is the critical path -- the busiest single node, since nodes run
    concurrently. `busy` is per engine and sums every node, so `busy` over
    `cycles` times the node count is that engine's utilisation.
    """

    cycles: int = 0
    busy: dict[str, int] = field(default_factory=dict)
    by_op: dict[str, int] = field(default_factory=dict)
    per_node: dict[tuple, int] = field(default_factory=dict)
    rounds: list[int] = field(default_factory=list)
    flops: int = 0
    mhz: float = 300.0

    #: Per node, the cycles that were FILL. Subtracted for `overlapped`.
    fill_node: dict[tuple, int] = field(default_factory=dict)

    @property
    def overlapped(self) -> int:
        """Cycles if every FILL hid behind the GEMM before it.

        L1 A is double-buffered (`l1_a_banks`), so the real machine hides most
        of the fill while this model charges all of it. The truth is between
        `overlapped` and `cycles`; the measured 81.2% of peak sits in that
        bracket, which is the honest claim this model can make.
        """
        return max(
            (c - self.fill_node.get(k, 0) for k, c in self.per_node.items()),
            default=0,
        )

    @property
    def seconds(self) -> float:
        return self.cycles / (self.mhz * 1e6)

    @property
    def gflops(self) -> float:
        return self.flops / self.seconds / 1e9 if self.cycles else 0.0

    def utilisation(self, engine: str, nodes: int) -> float:
        """Fraction of the critical path this engine's nodes spent busy."""
        if not self.cycles or not nodes:
            return 0.0
        return self.busy.get(engine, 0) / (self.cycles * nodes)

    def __str__(self) -> str:
        parts = " ".join(f"{k} {v:,}" for k, v in sorted(self.busy.items()))
        return (
            f"{self.cycles:,} cycles ({self.seconds * 1e3:.3f} ms at "
            f"{self.mhz:g} MHz) over {len(self.rounds)} rounds; busy {parts}"
        )


def _flits(elems: int, dtype: str) -> int:
    """Cycles to move `elems` values of `dtype` across one link."""
    return -(-elems * BITS.get(dtype, 16) // FLIT_BITS)


def cost_of_inst(inst, t: Target, vl: int = 0) -> int:
    """Cycles one instruction occupies its own node.

    A `vloop` is free itself; its body is charged by the caller, which is why
    this returns 0 for it rather than the loop's total.

    `vl` is the active vector length. Zero means VLMAX, which is what a caller
    that does not track VSETVL has to assume -- and overcharges by 2x on the
    VL=64 bands attention's softmax is made of.
    """
    n = vl or t.vlmax
    f = inst.fields
    match inst.op:
        case Opcode.FILL:
            return _flits(f["n"] * t.lanes * t.kblock, "fp16")
        case Opcode.GEMM:
            macs = (f["gm"] * t.lanes) * (f["nk"] * t.kblock) * (f["gn"] * t.lanes)
            return -(-macs // MACS_PER_CLUSTER)
        case Opcode.DRAIN:
            return _flits(f["n"] * t.lanes * t.lanes, f.get("dtype", "fp16"))
        case Opcode.VLD | Opcode.VST:
            elems = f.get("n", n)
            return max(
                _flits(elems, f.get("dtype", "fp16")), -(-elems // t.vector_lanes)
            )
        case Opcode.VALU | Opcode.VRED:
            return -(-n // t.vector_lanes)
        case Opcode.VLOOP:
            return 0
        case _:
            return 1


def _charge(res: Timing, inst, cycles: int) -> None:
    """Bill `cycles` to the instruction's node AND its engine.

    Per instruction, never per node: a matmul cluster and a vector core share
    mesh coordinates, so keying the engine off the node loses one of them.
    """
    key = (inst.engine.value, inst.node)
    res.per_node[key] = res.per_node.get(key, 0) + cycles
    res.busy[inst.engine.value] = res.busy.get(inst.engine.value, 0) + cycles
    res.by_op[inst.op.value] = res.by_op.get(inst.op.value, 0) + cycles
    if inst.op is Opcode.FILL:
        res.fill_node[key] = res.fill_node.get(key, 0) + cycles


def time_of(prog: Program, t: Target, flops: int = 0) -> Timing:
    """Cycles for `prog` on `t`, with `flops` for the achieved rate.

    Every node holds its own instruction stream and they run concurrently, so
    the critical path is the SLOWEST node rather than a sum. `prog.rounds` is
    not treated as a barrier: it slices the stream by flit budget, which says
    what the dispatcher can stage, not what depends on what.

    Pass `flops` from `cost_of(...).flops` to get `gflops`.
    """
    res = Timing(flops=flops, mhz=t.mhz)
    i = 0
    while i < len(prog.insts):
        inst = prog.insts[i]
        if inst.op is Opcode.VLOOP:
            span = inst.fields.get("body", 0)
            trips = max(1, inst.fields.get("count", 1))
            body = prog.insts[i + 1 : i + 1 + span]
            _charge(res, inst, sum(cost_of_inst(x, t) for x in body) * trips)
            i += 1 + span
            continue
        _charge(res, inst, cost_of_inst(inst, t))
        i += 1
    res.cycles = max(res.per_node.values(), default=0)
    res.rounds = [res.cycles]
    return res


@dataclass
class Serial:
    """Cycles when dependent BANDS cannot overlap. The other question.

    `time_of` asks how busy the busiest NODE is, which is right for one GEMM
    dealt across clusters and wrong for a chain: flash attention is
    GEMM -> softmax -> GEMM, each needing the one before it complete, so a
    global max over nodes hides the whole vector program behind the matmuls
    and reports that softmax is free. This sums the bands instead and takes the
    max over nodes WITHIN each, which is the other bound.

    Neither is the truth. `time_of` is a lower bound on a dependent program and
    this is an upper bound on an independent one; a real schedule with some
    overlap sits between them.
    """

    cycles: int = 0
    per_band: dict = field(default_factory=dict)
    by_engine: dict = field(default_factory=dict)
    mhz: float = 300.0

    @property
    def seconds(self) -> float:
        return self.cycles / (self.mhz * 1e6)

    def share(self, engine: str) -> float:
        """Fraction of the critical path spent in `engine`'s bands.

        The denominator that keeps an optimisation honest: a 20% saving in a
        part worth 18% of the program is a 3.6% saving.
        """
        if not self.cycles:
            return 0.0
        return self.by_engine.get(engine, 0) / self.cycles


def serial_of(prog: Program, t: Target) -> Serial:
    """Cycles for `prog` on `t` with bands run back to back.

    Needs `prog.bands`, which `codegen` records; a Program without it (one
    built by hand, or by an older codegen) returns zero rather than guessing
    boundaries out of the instruction stream, because guessing them wrong
    reads as a plausible number.

    VL is tracked per node across band boundaries, since codegen elides a
    VSETVL a core already holds and the length would otherwise read as VLMAX.
    """
    res = Serial(mhz=t.mhz)
    vl: dict = {}
    for name, lo, hi in prog.bands:
        per_node: dict = {}
        engine = ""
        i = lo
        while i < hi:
            inst = prog.insts[i]
            engine = engine or inst.engine.value
            if inst.op is Opcode.VSETVL:
                vl[inst.node] = inst.fields["vl"]
            n = vl.get(inst.node, 0)
            if inst.op is Opcode.VLOOP:
                span = inst.fields.get("body", 0)
                trips = max(1, inst.fields.get("count", 1))
                body = prog.insts[i + 1 : i + 1 + span]
                cost = sum(cost_of_inst(x, t, n) for x in body) * trips
                i += span
            else:
                cost = cost_of_inst(inst, t, n)
            per_node[inst.node] = per_node.get(inst.node, 0) + cost
            i += 1
        slowest = max(per_node.values(), default=0)
        res.per_band[name] = slowest
        res.by_engine[engine] = res.by_engine.get(engine, 0) + slowest
        res.cycles += slowest
    return res
