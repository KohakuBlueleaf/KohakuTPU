"""What one KohakuTPU statement costs, in cycles.

The project half of `kohakuaccel.analysis.timing`: that module owns the
accumulation and the bracket, this owns the per-statement figures. Every number
here is the machine's, not a tuning knob.
"""

from kohakuaccel.analysis.timing import Stage, time_of
from kohakutpu.hw import vector as V
from kohakutpu.isa import relayout as RL
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


# ------------------------------------------------- data movement, in credits
#: `docs/notes/data-movement-problem.md` §5, and they are RELATIVE credits per
#: byte -- no absolute rate is used or needed. From two ratios only: the widths
#: `w_S : w_M : w_L = 4 : 2 : 1`, and slow memory's locality penalty `rho ~ 15`.
SS, M_SEQ, LINK, M_RAND = 1, 2, 4, 30

#: §8. Units on an OPEN PATH -- not a ring, not a clique. `S` is the 2 MB MAG
#: store against one mesh's 4 GB, which is where the doc's `M/C ~ 1800` is from.
UNITS = 4


def move(nbytes: int, tier: str, walk: str = "seq", hops: int = 0) -> int:
    """Credits for moving `nbytes` over one path.

    A transfer is priced by its SLOW end: a fill out of DRAM into a scratchpad
    is slow-memory traffic whatever the scratchpad does, and irregular access
    inside `S` is free by construction.
    """
    if hops:
        return LINK * hops * nbytes
    if tier == "S":
        return SS * nbytes
    return (M_RAND if walk == "rand" else M_SEQ) * nbytes


def relayout_moves(made, count: int, staged: str | None, home: str = "M") -> list:
    """Every transfer one conversion makes, as ``(what, credits)``.

    `home` is the tier the BUFFER lives in and `staged` the tier the walk lands
    in, or None for a walk over the buffer itself. THE WHOLE POINT OF THE TIER
    IS HERE: the permuted side is the expensive one, and `S` -- where irregular
    access is free -- is where it stops costing 30 credits a byte.

    A buffer already IN `S` needs no staging at all: both sides are free of the
    locality penalty, so the conversion is one pass over `S` and nothing else.
    """
    n = made.nbytes * count
    if home == "S":
        return [("fill from S", move(n, "S")), ("drain into S", move(n, "S"))]
    # Whichever descriptor carries the walk is the non-sequential side, so
    # staging absorbs it only in `drain` mode -- a gather still reads M ragged.
    fills = "rand" if made.mode == "fill" else "seq"
    drains = "rand" if made.mode == "drain" else "seq"
    if staged is None:
        return [("fill", move(n, "M", fills)), ("drain", move(n, "M", drains))]
    return [
        ("fill", move(n, "M", fills)),
        ("drain into " + staged, move(n, staged, drains)),
        ("fill from " + staged, move(n, staged)),
        ("drain home", move(n, "M")),
    ]


def route_for(made, room: int = 0, home: str = "M") -> str | None:
    """Which tier this conversion should walk into, or None to walk in place.

    Cheapest under §5, which is NOT the fewest passes. Walking over the buffer
    is one pass and puts a non-sequential access on slow memory; staging in `S`
    is two passes and does not. MEASURED at 32 credits a byte against 6, and the
    doc's own guidance is that an extra local pass to avoid a ragged slow-memory
    access is almost always right.

    It helps only in `drain` mode: a `fill` mode walk reads the buffer ragged
    whatever it writes to, so staging there buys a pass and removes nothing.
    """
    if home == "S" and made.inplace:
        return None
    routes = []
    if made.inplace:
        routes.append((sum(c for _, c in relayout_moves(made, 1, None, home)), None))
    if room >= made.nbytes:
        routes.append((sum(c for _, c in relayout_moves(made, 1, "S", home)), "S"))
    if not routes:
        routes.append((sum(c for _, c in relayout_moves(made, 1, "M", home)), "M"))
    return min(routes, key=lambda r: r[0])[1]


def credits(compiled, room: int = 0, tier: str | None = "auto") -> dict:
    """What this call's byte-order changes cost in §5 credits.

    `room` is the staging capacity available, so `room=0` is a machine with no
    `S` to stage in. `tier` forces a route for analysis; the default routes each
    conversion the way the runtime would.

    A conversion this machine cannot walk is reported and NOT priced: it costs a
    host round trip, which is off this model's paths entirely -- the doc's links
    join units, and the host is not one.
    """
    out: dict = {"total": 0, "bytes": 0, "host": 0, "detail": []}
    for n, (_, name, before, after) in enumerate(compiled.conversions):
        if n in compiled.dead:
            continue
        got = RL.for_conversion(before, after, compiled.shape(name))
        if got is None:
            out["host"] += 1
            continue
        made, _, _, count = got
        home = "S" if compiled.tiers.get(name) == "l2" and room else "M"
        where = route_for(made, room, home) if tier == "auto" else tier
        moves = relayout_moves(made, count, where, home)
        n = sum(c for _, c in moves)
        out["total"] += n
        out["bytes"] += made.nbytes * count
        out["detail"].append((name, made.summary(), where, n, moves))
    return out


def link_credits(compiled, shards: int = UNITS) -> dict:
    """Cross-unit credits this call's conversions would spend at `shards` ways.

    §7 question 3. Zero means the shard axis SURVIVES every layout change here,
    which is the cheapest answer there is and is decidable at compile time.
    """
    out = {"credits": 0, "bytes": 0, "local": True}
    for n, (_, name, before, after) in enumerate(compiled.conversions):
        if n in compiled.dead:
            continue
        got = RL.for_conversion(before, after, compiled.shape(name))
        if got is None:
            continue
        made, _, _, count = got
        moved = RL.crossing(made, shards) * count
        out["bytes"] += moved
        out["credits"] += LINK * moved
        out["local"] &= RL.shard_local(made, shards)
    return out


def relayouts(compiled) -> list:
    """One :class:`Stage` per conversion, in the order they run.

    A conversion is NOT a statement, so `time_of` cannot see it: without this a
    plan reports identical cycles with and without the byte-order change it
    implies, and the cost model would let a relayout back in unnoticed. It is a
    stage of its own because it is a barrier -- the stage after it reads what it
    wrote.

    A conversion the machine cannot walk is charged NOTHING here and says so by
    its kind: what it really costs is a host round trip, which is not cycles on
    this machine at all.
    """
    out: list = []
    for n, (at, name, before, after) in enumerate(compiled.conversions):
        if n in compiled.dead:
            continue
        got = RL.for_conversion(before, after, compiled.shape(name))
        if got is None:
            out.append(Stage(at, "VC", 0, by_kind={"relayout:host": 0}))
            continue
        made, _, _, count = got
        n = RL.cycles_for(made, count)
        kind = "relayout" if made.inplace else "relayout:staged"
        out.append(Stage(at, "VC", n, per_unit={0: n}, by_kind={kind: n}))
    return out


def time(compiled, machine=None):
    """Cycles for `compiled`, as a :class:`~kohakuaccel.analysis.timing.Timing`.

    The conversions are spliced in ahead of the stage each is due before, so the
    total is what the machine really runs rather than the statements alone.
    """
    got = time_of(compiled, cost_for(compiled), machine, hidden, group=programs)
    due = relayouts(compiled)
    if not due:
        return got
    out: list = []
    for stage in got.stages:
        out += [s for s in due if s.index == stage.index]
        out.append(stage)
    got.stages = out + [s for s in due if s.index >= len(got.stages)]
    return got
