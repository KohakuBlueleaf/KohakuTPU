"""Level 1 -> level 1: cut a reduction wider than one vector pass into two.

The TREE folds up to VLMAX elements per pass and carries no partial across
passes (docs/isa/vector.md s6), so a GroupNorm row of 640 at VLMAX 128 has
nowhere to keep the running sum. Splitting the row into `(5, 128)` and reducing
twice puts that partial in a VALUE instead: the reshape is a view, so nothing
moves, and the second stage is a 128th of the first.

`sum` is the only kind whose association order changes, and blocked partials
keep more bits than one running total of 640, not fewer.
"""

from ktpu.ir.graph import REDUCTION, Graph, OpKind

#: Second-stage kind per first stage. `sumsq` has already squared, so folding
#: its chunk totals with `sumsq` again would square the partials.
SECOND = {
    OpKind.SUM: OpKind.SUM,
    OpKind.RMAX: OpKind.RMAX,
    OpKind.RMIN: OpKind.RMIN,
    OpKind.SUMSQ: OpKind.SUM,
}


def chunk_width(span: int, vlmax: int) -> int:
    """Largest divisor of `span` that fits one pass, or 0 if none does.

    A divisor rather than `vlmax` itself because the split is a RESHAPE, and an
    uneven last chunk would need a mask the tree has not got. Largest, so the
    second stage is as short as possible: 640 splits 5 x 128, not 128 x 5.
    """
    for d in range(min(span, vlmax), 1, -1):
        if span % d == 0:
            return d
    return 0


def _splice(graph: Graph, old, build):
    """Run `build` and move the ops it appended to where `old` sits.

    `Graph`'s builders append, and a rewrite in the middle of a straight-line
    program has to land in dependency order. Returns `build`'s result.
    """
    at = graph.ops.index(old)
    end = len(graph.ops)
    fresh = build()
    added = graph.ops[end:]
    del graph.ops[end:]
    graph.ops[at:at] = added
    return fresh


def split_wide(graph: Graph, vlmax: int) -> int:
    """Rewrite every reduction wider than `vlmax` into two narrower ones.

    Mutates `graph`; returns how many it split. Idempotent -- afterwards no
    reduction spans more than `vlmax`.

    Only a reduction over a CONTIGUOUS SUFFIX of the axes is split: the split is
    a reshape, and reshaping around an interior axis is not a view. Anything
    else is left for `lower` to reject with the message naming the real limit.
    """
    if vlmax < 2:
        return 0
    done = 0
    for op in list(graph.ops):
        if op.kind not in REDUCTION:
            continue
        x, axes = op.inputs[0], tuple(op.attrs["axes"])
        if axes != tuple(range(x.rank - len(axes), x.rank)):
            continue
        span = 1
        for a in axes:
            span *= x.shape[a]
        inner = chunk_width(span, vlmax) if span > vlmax else 0
        if not inner:
            continue
        outer = x.shape[: x.rank - len(axes)]

        def build(x=x, outer=outer, span=span, inner=inner, kind=op.kind, sh=op.out.shape):
            v = graph.reshape(x, (*outer, span // inner, inner))
            first = graph.reduce(kind, v, (v.rank - 1,), keepdim=False)
            fold = graph.reduce(SECOND[kind], first, (first.rank - 1,), keepdim=False)
            return graph.reshape(fold, sh) if fold.shape != sh else fold

        fresh = _splice(graph, op, build)
        for other in graph.ops:
            other.inputs = tuple(
                fresh if v.vid == op.out.vid else v for v in other.inputs
            )
        graph.outputs = [fresh if v.vid == op.out.vid else v for v in graph.outputs]
        graph.ops.remove(op)
        del graph._producer[op.out.vid]
        done += 1

    # A first stage of 65,536 leaves 512 chunk totals, which is still four
    # passes, so the rewrite has to reach its own output.
    return done + (split_wide(graph, vlmax) if done else 0)
