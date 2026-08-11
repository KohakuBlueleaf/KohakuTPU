"""Level 1 -> level 2: turn a Graph into a Schedule.

Assigns each op to an engine, fuses runs of elementwise work into one pass over
the data, and gives every band a tile and a grid.
"""

import itertools
import math
from dataclasses import dataclass, replace

from ktpu.ir.graph import REDUCTION, VIEW, Graph, OpKind
from ktpu.ir.sched import Band, Engine, Grid, SchedOp, Schedule, ScheduleError, Tile
from ktpu.passes import reduce as reduce_pass
from ktpu.passes import scalefold
from ktpu.passes.tile import choose_tile, epilogue_grid, grid_for
from ktpu.target import Target

_SOURCE = frozenset({OpKind.INPUT, OpKind.CONST})


#: Views that change neither which elements are read nor their order, so an
#: operand behind one is the same bytes as what fed it. PERMUTE reorders, SLICE
#: and PAD and CONCAT change the set -- each needs an address descriptor, so
#: resolving through them would read the wrong data at the right size.
TRANSPARENT = frozenset({OpKind.RESHAPE, OpKind.EXPAND})


def through_views(graph: Graph, v) -> tuple:
    """Resolve views on `v` to `(root, off, flipped, stride, run, pads, dims)`.

    Views move no data, so the operand a band reads is the ROOT tensor plus an
    address descriptor (vector-core.md s10). Element `i` of the logical span
    sits at `off + (i // run) * stride + i % run`: `(1, 1)` is contiguous,
    `(E, 1)` one element per row, `(E, W)` a `W`-wide band -- a GEGLU chunk, a
    conv filter tap, or one head of a packed projection.

    `flipped` records a transpose of the last two axes; for a matmul's B
    operand, already stored transposed, a flip means the region holds it in the
    orientation the GEMM wants and the packer must NOT transpose again. `pads`
    is the border a zero-pad put around the root before the window was cut, and
    the offset and stride are then in that PADDED frame -- the host has to
    build it, because no address generator here injects zeros. `dims` is the
    full `(extent, stride)` list, and `stride` is DEEP when the walk needs more
    levels than that: the host can still gather it, no engine can walk it.

    Raises ScheduleError on a view no descriptor covers at all: a CONCAT, or a
    pad of something that is already a view. Folding one away reads the wrong
    elements at the right size, a plausible wrong answer rather than an error.
    """
    try:
        return _outer_views(graph, v)
    except ScheduleError as simple:
        root, shape, strides, off, pads = _replay(graph, v)
        del simple
        flipped, stride, run, dims = _window(shape, strides)
        return root, off, flipped, stride, run, pads, dims


def _rowmajor(shape) -> list[int]:
    out, acc = [1] * len(shape), 1
    for i in range(len(shape) - 1, -1, -1):
        out[i] = acc
        acc *= shape[i]
    return out


def _merge(shape, strides) -> list[tuple[int, int]]:
    """`(extent, stride)` per axis, unit axes dropped and neighbours joined.

    Two axes are one run when the outer stride is exactly the inner axis's
    span, which is what makes `(N, H, W, C)` with C and W intact a single
    contiguous `W*C`.
    """
    dims: list[tuple[int, int]] = []
    for e, s in zip(reversed(shape), reversed(strides), strict=True):
        if e == 1:
            continue
        if dims and s == dims[-1][0] * dims[-1][1]:
            dims[-1] = (dims[-1][0] * e, dims[-1][1])
        else:
            dims.append((e, s))
    return list(reversed(dims))


def _reshape_strides(shape, strides, new) -> list[int] | None:
    """Strides for `new` over the same elements, or None if that is not a view.

    Consumes the merged axes innermost first: an axis of extent `e` takes the
    running stride, which then grows by `e`. A `new` extent that does not
    divide what is left crosses a discontinuity and has no stride at all.
    """
    dims = _merge(shape, strides)
    i = len(dims) - 1
    ext, st = dims[i] if i >= 0 else (1, 1)
    out = [0] * len(new)
    for j in range(len(new) - 1, -1, -1):
        while ext == 1 and i > 0:
            i -= 1
            ext, st = dims[i]
        if new[j] == 1:
            out[j] = ext * st
            continue
        if ext % new[j]:
            return None
        out[j] = st
        st *= new[j]
        ext //= new[j]
    return out if ext == 1 and i <= 0 else None


#: `stride` for a view that no address generator here can walk: its rows are
#: themselves gapped, so it takes three levels. The host can still gather it.
DEEP = -1


def _window(shape, strides) -> tuple[bool, int, int, list[tuple[int, int]]]:
    """`(flipped, stride, run, dims)` for a row-major walk of a strided view.

    `dims` is the full `(extent, stride)` list, outermost first, which the host
    can always walk. `stride` is DEEP when that takes more than the offset,
    run and stride an address generator carries.
    """
    for flip in (False, True):
        sh, st = list(shape), list(strides)
        if flip:
            if len(sh) < 2:
                continue
            sh[-2], sh[-1] = sh[-1], sh[-2]
            st[-2], st[-1] = st[-1], st[-2]
        dims = _merge(sh, st)
        if not dims:
            return flip, 1, 1, [(1, 1)]
        if len(dims) == 1 and dims[0][1] == 1:
            return flip, 1, 1, dims
        if len(dims) == 1 and not flip:
            return flip, dims[0][1], 1, dims
        if len(dims) == 2 and dims[1][1] == 1:
            return flip, dims[0][1], dims[1][0], dims
    return False, DEEP, 0, _merge(list(shape), list(strides))


def _replay(graph: Graph, v) -> tuple[object, list[int], list[int], int]:
    """Walk `v` back to its root, then forward as `(root, shape, strides, off)`.

    Strides and offset are in ROOT elements. Raises ScheduleError on a view no
    stride list expresses: a non-zero pad, whose border is not in the root at
    all, or a concat, which joins two regions and is a view of neither.
    """
    chain = []
    op = graph.producer(v)
    while op.kind in VIEW:
        if op.kind is OpKind.CONCAT:
            raise ScheduleError(
                f"%{op.out.vid} is a concat of {len(op.inputs)} values; it is a "
                "view of none of them, so it needs a band that copies"
            )
        chain.append(op)
        op = graph.producer(op.inputs[0])

    root = op.out
    shape, strides, off = list(root.shape), _rowmajor(root.shape), 0
    pads, flat = None, False
    for o in reversed(chain):
        if flat and o.kind is not OpKind.RESHAPE:
            raise ScheduleError(
                f"%{o.out.vid} is a {o.kind.value} of a window whose axes no "
                "longer line up with its shape; move it before the reshape"
            )
        match o.kind:
            case OpKind.RESHAPE:
                new = list(o.out.shape)
                st = _reshape_strides(shape, strides, new)
                if st is not None:
                    shape, strides = new, st
                    continue
                # A reshape never reorders elements, so the merged dims survive
                # one that is not a view; only the axis boundaries are lost.
                dims = _merge(shape, strides)
                shape = [e for e, _ in dims]
                strides = [s for _, s in dims]
                flat = True
            case OpKind.PERMUTE:
                order = o.attrs["order"]
                shape = [shape[i] for i in order]
                strides = [strides[i] for i in order]
            case OpKind.EXPAND:
                new = list(o.out.shape)
                strides = [0 if n != e else s for e, n, s in zip(shape, new, strides)]
                shape = new
            case OpKind.SLICE:
                begin, end = o.attrs["begin"], o.attrs["end"]
                off += sum(b * s for b, s in zip(begin, strides))
                shape = [e - b for b, e in zip(begin, end)]
            case OpKind.PAD:
                if not any(lo or hi for lo, hi in o.attrs["pads"]):
                    continue
                # The border is not in the root, so the descriptor cannot reach
                # it: the padded frame has to exist before anything indexes it.
                if pads is not None or off or strides != _rowmajor(shape):
                    raise ScheduleError(
                        f"%{o.out.vid} pads a value that is already a view; the "
                        "zeros would have to be injected mid-walk, which is "
                        "mx_tdesc's `valid` bit and no address generator here "
                        "carries it"
                    )
                pads = tuple(o.attrs["pads"])
                shape = list(o.out.shape)
                strides = _rowmajor(shape)
    return root, shape, strides, off, pads


def _outer_views(graph: Graph, v) -> tuple[object, int, bool, int, int]:
    """`through_views` for outer-axis slices, single columns and a transpose."""
    op = graph.producer(v)
    off, flipped, stride = 0, False, 1
    while True:
        if op.kind in TRANSPARENT:
            op = graph.producer(op.inputs[0])
        elif op.kind is OpKind.SLICE:
            src = op.inputs[0]
            begin, end = op.attrs["begin"], op.attrs["end"]
            inner = 1
            for d in src.shape[1:]:
                inner *= d
            outer_only = not any(
                b or e != d
                for b, e, d in zip(begin[1:], end[1:], src.shape[1:], strict=True)
            )
            column = (
                src.rank == 2
                and begin[0] == 0
                and end[0] == src.shape[0]
                and end[1] - begin[1] == 1
            )
            if outer_only:
                off += begin[0] * inner
            elif column and stride == 1:
                off += begin[1]
                stride = src.shape[1]
            else:
                raise ScheduleError(
                    f"%{op.out.vid} slices an inner axis to width "
                    f"{end[-1] - begin[-1]}; only an outer-axis slice or a "
                    "single column is expressible as offset plus stride"
                )
            op = graph.producer(src)
        elif op.kind is OpKind.PERMUTE:
            order = list(op.attrs["order"])
            rank = len(order)
            if order != [*range(rank - 2), rank - 1, rank - 2]:
                raise ScheduleError(
                    f"%{op.out.vid} permutes more than the last two axes; "
                    "that needs a stride list level 3 does not carry"
                )
            flipped = not flipped
            op = graph.producer(op.inputs[0])
        else:
            break
    if op.kind in VIEW:
        raise ScheduleError(
            f"%{op.out.vid} is a {op.kind.value}, which needs an address "
            "descriptor level 3 does not carry yet; folding it away would read "
            "the wrong elements at the right size"
        )
    return op.out, off, flipped, stride, 1, None, None


def shared_a_blocking(graph: Graph, target: Target) -> dict[int, tuple[int, int]]:
    """Common `(tile.m, tile.k)` for every A operand more than one matmul reads.

    An operand is packed in L1-entry order FOR A TILE, blocked by
    `(tile.m, tile.k)`. A region holds one layout, so two matmuls sharing an A
    operand and choosing different blocking cannot both read it -- whichever
    was packed last wins and the other reads the right bytes in the wrong
    places. MoE hits this: `x` feeds the gate and every expert.

    Takes the smallest blocking in each group, which is always safe: a smaller
    tile means more grid instances and still covers the output, and a smaller
    `nk` only relaxes the L1 capacity bound. `tile.n` is unconstrained, since it
    blocks the B side and each matmul has its own B.

    Returns A value id -> `(m, k)`, only for operands read by two or more.
    """
    seen: dict[int, list[tuple[int, int]]] = {}
    for op in graph.ops:
        if op.kind is not OpKind.MATMUL:
            continue
        a, b = op.inputs
        choice = choose_tile(a.shape[-2], a.shape[-1], b.shape[-1], target)
        try:
            view = through_views(graph, a)
        except ScheduleError:
            continue
        # A windowed operand gets a region of its own, so it shares with nothing.
        key = a.vid if view[3] != 1 else view[0].vid
        seen.setdefault(key, []).append((choice.tile.m, choice.tile.k))
    return {
        v: (min(m for m, _ in ts), min(k for _, k in ts))
        for v, ts in seen.items()
        if len(ts) > 1
    }


@dataclass(frozen=True)
class Relayed:
    """A value the scheduler invented, standing in for a graph Value.

    A matmul reading another matmul's output needs its tile re-laid into entry
    order first, so lower() inserts a band and the GEMM takes ITS result. That
    result has no level-1 value, but everything downstream asks the operand for
    `vid`, `shape` and `numel`.
    """

    vid: int
    shape: tuple[int, ...]
    numel: int


def widest_shape(graph: Graph, ops: list[SchedOp]) -> tuple[int, ...]:
    """Logical shape of the largest value `ops` read or write.

    A band's broadcast decisions are made against what it WALKS, which for a
    reduction is its input rather than its narrower output.
    """
    shapes = {v.vid: tuple(v.shape) for op in graph.ops for v in (*op.inputs, op.out)}
    seen = [v for o in ops for v in (*o.ins, o.out) if v in shapes]
    return max((shapes[v] for v in seen), key=lambda s: math.prod(s), default=())


def mm_elems(band: Band) -> int:
    """Output elements of a matmul band, from the whole-problem `m` and `n`."""
    a = band.ops[0].attrs
    return a["m"] * a["n"]


def externals(
    graph: Graph, ops: list[SchedOp], producer: Band | None, origin: dict[int, str]
) -> dict[int, tuple[str, object]]:
    """Values `ops` read that none of them produces, as vid -> (what, which).

    `("band", name)` is the matmul whose output this run consumes,
    `("input", name)` a graph input the vector core must load itself,
    `("const", value)` a literal that becomes a scalar register, and
    `("value", name)` a value an EARLIER band produced -- flash attention's
    running max is one, since the band that reduced it is not the band that
    subtracts it. `producer` is the preceding matmul band and `origin` maps
    every already-scheduled value to the band that made it.

    Views are transparent: a reshape or permute moves no data, so an operand
    behind one resolves to whatever fed the view. Raises ScheduleError if a
    value's producer is none of these, which means codegen has no way to name it.
    """
    made = {o.out for o in ops}
    out: dict[int, tuple[str, object]] = {}
    for o in ops:
        for vid in o.ins:
            if vid in made or vid in out:
                continue
            try:
                src = graph.producer_of(vid)
            except KeyError:  # a value lower() invented, e.g. a relayout
                out[vid] = ("value", origin[vid])
                continue
            while src.kind in VIEW:
                src = graph.producer(src.inputs[0])
            match src.kind:
                case OpKind.CONST:
                    out[vid] = ("const", src.attrs["value"])
                case OpKind.INPUT:
                    out[vid] = ("input", src.attrs.get("name") or f"%{vid}")
                case OpKind.MATMUL if src.out.vid in origin:
                    out[vid] = ("band", origin[src.out.vid])
                case OpKind.MATMUL if producer is not None:
                    out[vid] = ("band", producer.name)
                case _ if src.out.vid in origin:
                    out[vid] = ("value", f"{origin[src.out.vid]}:%{src.out.vid}")
                case _:
                    raise ScheduleError(
                        f"value %{vid} is produced by {src.kind.value}, which "
                        "this band cannot name as an operand"
                    )
    return out


def engine_for(kind: OpKind) -> Engine | None:
    """Which engine runs `kind`, or None if it costs nothing at run time.

    Views are None: a reshape or permute folds into the consumer's address
    generator and moves no data.
    """
    if kind in _SOURCE or kind in VIEW:
        return None
    if kind is OpKind.MATMUL:
        return Engine.MATMUL
    return Engine.VECTOR


def op_span(op) -> int:
    """Elements one vector op sweeps -- a reduction is sized by its INPUT."""
    return op.inputs[0].numel if op.kind in REDUCTION else op.out.numel


def _group_by_span(graph: Graph, seg: list, after: int | None = None) -> list:
    """Reorder one run of vector ops so equal-span work is adjacent.

    A band breaks whenever the element count changes, so a small op scheduled
    between two large ones costs two boundaries and a store/reload of the large
    tile. Flash attention does exactly that: `ell * corr` is [rows,1] and lands
    between `exp2` and `p.sum()`, both [rows,block], which is why the steady
    state emits {sub,mul,exp2} {mul} {sum} where the first iteration emits one
    fused {sub,mul,exp2,sum}.

    Greedy list schedule: among the ops whose inputs are ready, prefer one whose
    span matches the group being built, else take the earliest. Dependencies are
    resolved THROUGH VIEWS -- a reshape is not in `seg`, so matching on the view
    itself would miss the edge and reorder past a real dependency.
    """
    if len(seg) < 3:
        return seg
    at = {o.out.vid: i for i, o in enumerate(seg)}

    def roots(op):
        out = set()
        for v in op.inputs:
            try:
                r = through_views(graph, v)[0]
            except (KeyError, AttributeError):
                r = v
            if (i := at.get(getattr(r, "vid", None))) is not None:
                out.add(i)
        return out

    deps = [roots(o) for o in seg]
    nleft = [len(d) for d in deps]
    users: list[list[int]] = [[] for _ in seg]
    for i, d in enumerate(deps):
        for j in d:
            users[j].append(i)

    # Only the FIRST band after a matmul can fold into it as an epilogue, so the
    # run has to start with an op that reads the matmul. Leading with anything
    # else costs a whole band, which is how the causal kernel got LONGER before
    # this bias existed: 726 -> 738 instructions.
    def reads_mm(op):
        return after is not None and any(v.vid == after for v in op.inputs)

    done = [False] * len(seg)
    order, cur = [], None
    for step in range(len(seg)):
        ready = [i for i in range(len(seg)) if not done[i] and nleft[i] == 0]
        if step == 0 and after is not None:
            pick = next((i for i in ready if reads_mm(seg[i])), ready[0])
        else:
            pick = next((i for i in ready if op_span(seg[i]) == cur), ready[0])
        done[pick] = True
        cur = op_span(seg[pick])
        order.append(seg[pick])
        for u in users[pick]:
            nleft[u] -= 1
    return order


def fuse_order(graph: Graph, enable: bool = True) -> list:
    """`graph.ops` reordered for band fusion, matmuls acting as barriers.

    Matmuls are not moved: `lower` resolves a vector band's operands against
    `last_mm`, so reordering them would change which band an external resolves
    to. Only the elementwise runs between them are rescheduled.

    ON, and it was disabled on the wrong metric. At L=128 D=64 block=64 it
    costs CAUSAL eight instructions, 531 -> 539, which is what kept it off --
    but it takes causal's vector cycles 2,043 -> 1,645, a 19.5% WIN, with the
    arithmetic identical either way.

    One cause for both. Fusing a run whose natural grid is 4 into a band that
    reduces puts it on the reduction's grid of 8, so the work is issued as
    eight core programs rather than four AND runs on eight cores rather than
    four: instructions up, wall clock halved. Idle core-slots across causal's
    vector bands go 29/88 -> 14/64.
    """
    ops = [o for o in graph.ops if engine_for(o.kind) is not None]
    if not enable:
        return ops
    out: list = []
    seg: list = []
    after: int | None = None
    for o in ops:
        if o.kind is OpKind.MATMUL:
            out += _group_by_span(graph, seg, after)
            out.append(o)
            seg, after = [], o.out.vid
        else:
            seg.append(o)
    return out + _group_by_span(graph, seg, after)


def lower(
    graph: Graph,
    target: Target,
    vector_tile: int = 1024,
    fold_epilogue: bool = True,
    reorder: bool = True,
    absorb_scale: bool = True,
    split_reductions: bool = True,
) -> Schedule:
    """Lower `graph` onto `target`, returning a Schedule.

    Each matmul becomes a MATMUL band tiled by `choose_tile` against the whole
    problem. Maximal runs of elementwise ops are fused into one VECTOR band,
    since a pass doing one op per element is memory-bound and one doing several
    is not.

    With `fold_epilogue`, an elementwise run reading only the preceding matmul
    becomes a band that `consumes` it: same tiles, same order, same FP16.

    It changes no precision. The accumulator converts to FP16 at stage 6 on
    EMIT (`mx_acu_fp.v`) and a DRAIN writes FP16 (isa/cluster.md s5), so the
    intermediate is FP16 folded or not, and the clamp at 65504 happens in the
    accumulator either way. It does not save the trip through DRAM either:
    nothing moves a tile from a cluster to a vector core (vector-core.md s9).

    What it buys is the grid: the epilogue walks the producer's tiles in the
    producer's order, so no re-tiling pass is needed. A reduction cannot fold,
    because it needs the whole output.

    `vector_tile` is elements per vector grid instance. Raises ScheduleError via
    `choose_tile`/`grid_for` if a matmul does not fit the target.

    `absorb_scale` runs `scalefold.absorb` and `split_reductions` runs
    `reduce.split_wide`; both REWRITE `graph` in place and both are idempotent,
    so lowering the same graph twice agrees.
    """
    if split_reductions:
        reduce_pass.split_wide(graph, target.vlmax)
    if absorb_scale:
        scalefold.absorb(graph)
    shared = shared_a_blocking(graph, target)
    sched = Schedule(name=graph.name)
    vid_gen = itertools.count(max((o.out.vid for o in graph.ops), default=0) + 1)
    pending: list[SchedOp] = []
    pending_elems = 0
    last_mm: Band | None = None
    mm_out: int | None = None
    chain_src: set[int] = set()
    origin: dict[int, str] = {}
    seen_elems: dict[int, int] = {}
    seen_windows: dict[int, dict] = {}

    def flush() -> None:
        nonlocal pending, pending_elems, last_mm, mm_out
        if not pending:
            return
        foldable = (
            fold_epilogue
            and last_mm is not None
            and mm_out is not None
            and mm_out in chain_src
            and last_mm.grid.sk == 1
            and pending_elems == mm_elems(last_mm)
            and not any(o.kind in REDUCTION for o in pending)
        )
        for o in pending:
            o.engine = Engine.VECTOR
        red = next((o for o in pending if o.kind in REDUCTION), None)
        if foldable:
            grid, tile = epilogue_grid(last_mm, target)
        elif red is not None:
            cols = max(1, red.attrs.get("red", 1))
            if cols > target.vlmax:
                raise ScheduleError(
                    f"{red.kind.value} reduces {cols} elements and VLMAX is "
                    f"{target.vlmax}; a row wider than one pass needs the TREE "
                    "to carry a partial across passes, which is not emitted yet"
                )
            rows = max(1, pending_elems // cols)
            n_inst = max(1, min(target.vector_cores, rows))
            grid = Grid(m=n_inst)
            tile = Tile(m=-(-rows // n_inst), n=cols)
        else:
            grid = Grid(m=max(1, -(-pending_elems // vector_tile)))
            tile = Tile(m=min(vector_tile, pending_elems))
        band = sched.add(
            Band(
                engine=Engine.VECTOR,
                grid=grid,
                tile=tile,
                ops=pending,
                consumes=last_mm.name if foldable else None,
                ext=externals(graph, pending, last_mm, origin),
                elems=dict(seen_elems),
                windows={
                    v: w
                    for v, w in seen_windows.items()
                    if any(v in o.ins for o in pending)
                },
                shape=widest_shape(graph, pending),
            )
        )
        for o in pending:
            origin[o.out] = band.name
        pending, pending_elems = [], 0
        seen_elems.clear()
        seen_windows.clear()
        last_mm, mm_out = None, None

    for op in fuse_order(graph, reorder):
        eng = engine_for(op.kind)
        if eng is None:
            continue

        if eng is Engine.MATMUL:
            flush()
            a, b = op.inputs
            m, k = a.shape[-2], a.shape[-1]
            n = b.shape[-1]
            choice = choose_tile(m, k, n, target)
            views = [through_views(graph, v) for v in op.inputs]
            # A FILL walks one contiguous span, so a windowed operand is named
            # by the VIEW and gathered into a region of its own before the GEMM.
            gathered = {
                v.vid: {
                    "off": r[1],
                    "dims": r[6] if r[6] is not None else [(v.numel, r[3])],
                    "shape": (v.numel // v.shape[-1], v.shape[-1]),
                    "pad": r[5],
                }
                for v, r in zip(op.inputs, views, strict=True)
                if r[3] != 1 or r[5] is not None
            }
            ins = tuple(
                v if v.vid in gathered else r[0]
                for v, r in zip(op.inputs, views, strict=True)
            )
            offs = tuple(0 if v.vid in gathered else r[1] for v, r in zip(ins, views))
            # A DRAIN writes sub-tile order, a FILL reads L1-entry order, and at
            # bflip=0 B also wants a transpose no engine here does.
            if views[1][0].vid in origin:
                raise ScheduleError(
                    f"the B operand %{b.vid} comes from band "
                    f"{origin[views[1][0].vid]}, and only a HOST-PACKED operand "
                    "is in L1-entry order. Bring it back and pass it in, or "
                    "give the value a relayout band"
                )
            for side, v, r in (("A", a, views[0]), ("B", b, views[1])):
                if r[3] == DEEP and r[0].vid in origin:
                    raise ScheduleError(
                        f"the {side} operand %{v.vid} walks {r[6]} of "
                        f"%{r[0].vid}, which a band computes; that is three "
                        "levels and only the host gathers those"
                    )
                if (r[3] != 1 or r[5]) and r[0].vid in origin and side == "B":
                    raise ScheduleError(
                        f"the B operand %{v.vid} is a {r[4]}-wide window of "
                        f"%{r[0].vid}, which a band computes; only the A side "
                        "has a relay that lays one out. Slice the weight "
                        "instead, so the window is over what the host packs"
                    )
                if r[5] and r[0].vid in origin:
                    raise ScheduleError(
                        f"the {side} operand %{v.vid} is a window of a PADDED "
                        f"%{r[0].vid}, which a band computes; the border exists "
                        "only in the host's copy, so the value has to come back "
                        "and go out padded"
                    )
            src_root = views[0][0]
            if src_root.vid in origin and (
                graph.producer(src_root).kind is OpKind.MATMUL or a.vid in gathered
            ):
                # A DRAIN writes sub-tile order, a FILL reads L1-entry order,
                # and only a vector store converts. Relayout through one.
                n_el = a.numel
                gathered.pop(a.vid, None)
                fresh = next(vid_gen)
                relay = SchedOp(
                    OpKind.MUL,
                    {},
                    engine=Engine.VECTOR,
                    ins=(src_root.vid, next(vid_gen)),
                    out=fresh,
                    offs=(views[0][1], 0),
                    strides=(views[0][3], 1),
                    runs=(views[0][4], 1),
                )
                sched.add(
                    Band(
                        engine=Engine.VECTOR,
                        grid=Grid(m=max(1, -(-n_el // vector_tile))),
                        tile=Tile(m=min(vector_tile, n_el)),
                        ops=[relay],
                        ext={
                            src_root.vid: ("value", origin[src_root.vid]),
                            relay.ins[1]: ("const", 1.0),
                        },
                        elems={src_root.vid: src_root.numel, fresh: n_el},
                        shape=tuple(a.shape),
                        name=f"relay{fresh}",
                    )
                )
                origin[fresh] = f"relay{fresh}"
                ins = (Relayed(fresh, a.shape, n_el), ins[1])
                offs = (0, offs[1])
            if ins[0].vid in shared:
                tm, tk = shared[ins[0].vid]
                choice = replace(choice, tile=replace(choice.tile, m=tm, k=tk))
            mm = SchedOp(
                OpKind.MATMUL,
                {"m": m, "k": k, "n": n, "bflip": views[1][2]},
                engine=Engine.MATMUL,
                ins=tuple(v.vid for v in ins),
                out=op.out.vid,
                offs=offs,
            )
            ext = externals(graph, [mm], None, origin)
            for vid, spec in gathered.items():
                ext[vid] = ("window", (*ext[vid], spec))
            band = Band(
                engine=Engine.MATMUL,
                grid=grid_for(m, n, choice),
                tile=choice.tile,
                ops=[mm],
                residency={"b": True},
                ext=ext,
                elems={v.vid: v.numel for v in (*ins, op.out)},
                shape=tuple(op.out.shape),
            )
            sched.add(band)
            origin[op.out.vid] = band.name
            last_mm, mm_out = band, op.out.vid
            chain_src = set()
        else:
            span = op.inputs[0].numel if op.kind in REDUCTION else op.out.numel
            if pending and span != pending_elems:
                flush()
            views = [through_views(graph, v) for v in op.inputs]
            if any(x[5] for x in views):
                raise ScheduleError(
                    f"{op.kind.value} %{op.out.vid} reads a padded view; a "
                    "vector load has bounds but no zero injection, so the pad "
                    "has to be a GEMM operand the host builds or not exist"
                )
            if any(x[3] == DEEP for x in views):
                bad = next(x for x in views if x[3] == DEEP)
                raise ScheduleError(
                    f"{op.kind.value} %{op.out.vid} walks {bad[6]} of "
                    f"%{bad[0].vid}: three levels, and vec_agu carries an "
                    "offset, a run and one stride"
                )
            # A window is named by the VIEW: both halves of a GEGLU projection
            # resolve to the same root, and one vid cannot be two operands.
            ins = tuple(
                v if r[3] != 1 else r[0]
                for v, r in zip(op.inputs, views, strict=True)
            )
            for v, r in zip(op.inputs, views, strict=True):
                if r[3] != 1:
                    seen_windows[v.vid] = {
                        "root": r[0].vid,
                        "root_elems": r[0].numel,
                        "off": r[1],
                        "stride": r[3],
                        "run": r[4],
                    }
            chain_src.update(v.vid for v in ins)
            attrs = dict(op.attrs)
            if op.kind in REDUCTION:
                shape = op.inputs[0].shape
                attrs["red"] = 1
                for a in op.attrs["axes"]:
                    attrs["red"] *= shape[a]
                # One band, one reduction width: `flush` sizes the tile from the
                # first reduction it finds and the rest would run at the wrong VL.
                if any(
                    o.kind in REDUCTION and o.attrs.get("red") != attrs["red"]
                    for o in pending
                ):
                    flush()
            pending.append(
                SchedOp(
                    op.kind,
                    attrs,
                    ins=tuple(v.vid for v in ins),
                    out=op.out.vid,
                    offs=tuple(x[1] for x in views),
                    strides=tuple(x[3] for x in views),
                    runs=tuple(x[4] for x in views),
                )
            )
            seen_elems.update({v.vid: v.numel for v in ins})
            seen_elems.update({w["root"]: w["root_elems"] for w in seen_windows.values()})
            seen_elems[op.out.vid] = op.out.numel
            pending_elems = span

    flush()
    # A result that is a pure reshape of a band's output IS that output: no
    # band drains the view, and conv2d ends on one.
    sched.outputs = []
    for v in graph.outputs:
        root, off, _flip, stride, _run, pads, _dims = through_views(graph, v)
        same = stride == 1 and not off and not pads and root.numel == v.numel
        if root.vid != v.vid and root.vid not in origin and v.vid not in origin:
            raise ScheduleError(
                f"%{v.vid} is returned but every op above it is a view of "
                f"%{root.vid}, so no band computes it and nothing drains it. A "
                "view moves no data -- an upsample written as reshape, expand "
                "and reshape lands here, and duplication needs an engine"
            )
        sched.outputs.append(root.vid if same and root.vid in origin else v.vid)
    sched.verify()
    return sched


def fusion_report(sched: Schedule) -> list[str]:
    """One line per band: engine, grid, and ops per element.

    Ops per element is what decides whether a vector band is memory- or
    compute-bound -- below 2 the NoC ports run out before the ALUs do.
    """
    out = []
    for b in sched.bands:
        if b.engine is Engine.VECTOR:
            bound = "compute" if len(b.ops) >= 2 else "MEMORY"
            out.append(
                f"{b.name:<4} vector  grid{b.grid}  {len(b.ops)} ops/elem  {bound}-bound"
            )
        else:
            mm = b.ops[0].attrs
            epi = [o for o in b.ops[1:] if o.engine is Engine.VECTOR]
            line = (
                f"{b.name:<4} matmul  grid{b.grid}  tile {b.tile}  "
                f"{mm['m']}x{mm['k']}x{mm['n']}"
            )
            if epi:
                line += (
                    f"\n     +vector epilogue, {len(epi)} ops on the resident tile"
                    " (no DRAM round trip)"
                )
            out.append(line)
    return out
