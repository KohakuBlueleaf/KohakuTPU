"""What one KohakuTPU statement costs, in cycles.

The project half of `kohakuaccel.analysis.timing`: that module owns the
accumulation and the bracket, this owns the per-statement figures. Every number
here is the machine's, not a tuning knob.
"""

from kohakuaccel.analysis.timing import time_of
from kohakutpu.hw import vector as V
from kohakutpu.lang import backend as B

from kohakutpu import layout as LO

#: MACs a cluster retires per cycle: 4 TCU of 4x8x4, one sub-tile x 32 K.
MACS_PER_CLUSTER = 512

#: Payload bits in one 288-bit flit, so `b` bits occupy `b / FLIT_BITS` cycles.
FLIT_BITS = 256


def flits(elems: int, bits: int = 16) -> int:
    return -(-elems * bits // FLIT_BITS)


def hidden(stmt) -> bool:
    """Whether a unit overlaps this with the work before it.

    L1 A is double-buffered, so the machine hides most of a fill behind the
    GEMM ahead of it while the model charges all of it.
    """
    return stmt.kind == "fill"


def cost_for(compiled):
    """A `cost(run, machine)` closed over one compilation.

    `run` is the statements of ONE program. An elementwise pass is sized by what
    one instance really walks, which only the compilation knows: `Buffer.parts`
    counts logical elements and the pass walks the padded image.
    """
    per = B._per(compiled)

    def span_of(stmt) -> int:
        try:
            return B._span(compiled, stmt, per)
        except (KeyError, AttributeError):
            return per

    def band(run) -> int:
        """A vector program: ALU words, the L1 traffic each CHUNK repeats, and
        one fill per operand and drain per region.

        The L1 term is not optional -- a chunk loads every operand and stores
        every result, so a chain of `n` ALU words really issues `n + slots +
        regions` instructions. Counting the chain alone undercounted a softmax
        1,792 against the simulator's 3,166.
        """
        alu = 0
        for s in run:
            steps = len(s.args.get("chain", ())) + sum(
                len(ops) for ops in s.args.get("lifted", ())
            )
            alu += max(1, steps) * -(-span_of(s) // V.LANES)
        span = max((span_of(s) for s in run), default=per)
        slots, regions = len(B._slots(run)), len(set(B._regions(run)))
        touch = (slots + regions) * -(-span // V.LANES)
        return alu + touch + (slots + regions) * flits(span)

    def cost(run, machine=None) -> int:
        run = run if isinstance(run, list) else [run]
        if run[0].args.get("resident"):
            # The tile arrived over the NoC, so this pays ALU and one drain --
            # never a fill, which is the whole point of fusing it.
            steps = len(run[0].args.get("chain", ()))
            span = run[0].args["gm"] * run[0].args["gn"] * LO.LANES * LO.LANES
            return max(1, steps) * -(-span // V.LANES) + flits(span)
        if run[0].kind in ("apply", "reduce"):
            return band(run)
        stmt = run[0]
        a = stmt.args
        match stmt.kind:
            case "fill":
                return flits(a["groups"] * a["blocks"] * LO.LANES * LO.KBLOCK)
            case "gemm":
                macs = (
                    (a["gm"] * LO.LANES) * (a["nk"] * LO.KBLOCK) * (a["gn"] * LO.LANES)
                )
                return -(-macs // MACS_PER_CLUSTER)
            case "drain":
                return flits(a["gm"] * a["gn"] * LO.LANES * LO.LANES)
            case _:
                return 1

    return cost


def programs(compiled, stage, stmts) -> list:
    """An instance's statements as the PROGRAMS they run as.

    A run of chain statements the descriptor budget admits is one program; the
    emitter cuts it exactly here, so the cost model and the bytes agree.
    """
    out: list = []
    run: list = []
    for s in stmts:
        if not B._bandable(s):
            if run:
                out.append(run)
            out += [[s]]
            run = []
            continue
        if run and B._cannot(compiled, [*run, s]) is not None:
            out.append(run)
            run = []
        run.append(s)
    if run:
        out.append(run)
    return out


def time(compiled, machine=None):
    """Cycles for `compiled`, as a :class:`~kohakuaccel.analysis.timing.Timing`."""
    return time_of(compiled, cost_for(compiled), machine, hidden, group=programs)
