"""Level 1 -> level 1: absorb a constant factor into the scale above it.

`exp2((s - m) * log2 e)` costs one vector pass more than `exp2(s - m)` does,
and the factor does not have to be there: `s` already arrives from a multiply
by `1/sqrt(d)`, and scaling BOTH `s` and `m` by the same positive constant
leaves the softmax unchanged. So the factor moves up into that multiply and the
pass disappears.

The win is one op per element on every band that exponentiates, which is the
only kind of saving that moves cycles -- `vec_lanes` retires 16 element-ops per
cycle whatever the mode, so fusing passes onto the chain wire does not.
Measured with `scripts/py/isa_study.py`.
"""

from ktpu.ir.graph import Graph, Op, OpKind, Value

LOG2_E = 1.4426950408889634

#: Ops whose output scales by C when every input does. `max`/`min` qualify only
#: because the scale is POSITIVE; `sumsq` scales by C squared, so it is absent.
COVARIANT = frozenset(
    {
        OpKind.ADD,
        OpKind.SUB,
        OpKind.NEG,
        OpKind.MAX,
        OpKind.MIN,
        OpKind.RMAX,
        OpKind.RMIN,
        OpKind.SUM,
    }
)


def _const_operand(graph: Graph, op: Op) -> tuple[Value, Op] | None:
    """The constant operand of `op` and its producer, or None if it has none."""
    for v in op.inputs:
        p = graph.producer(v)
        if p.kind is OpKind.CONST:
            return v, p
    return None


def _readers(graph: Graph) -> dict[int, list[Op]]:
    out: dict[int, list[Op]] = {}
    for op in graph.ops:
        for v in op.inputs:
            out.setdefault(v.vid, []).append(op)
    return out


def _cone(graph: Graph, src: Value) -> tuple[set[int], dict[int, Op], list[str]]:
    """Values that must scale with `src`, and the multiplies that would scale them.

    Walks backward through positively homogeneous ops and stops at a multiply
    by a constant, which is where the factor can be absorbed. Returns
    `(scaled vids, anchors by id, reasons)`; a non-empty reason list means the
    walk left the region a uniform scale can be pushed through, and the caller
    must not fold.
    """
    scaled: set[int] = set()
    anchors: dict[int, Op] = {}
    why: list[str] = []
    stack, seen = [src], set()
    while stack:
        v = stack.pop()
        if v.vid in seen:
            continue
        seen.add(v.vid)
        p = graph.producer(v)
        if p.kind is OpKind.MUL and _const_operand(graph, p) is not None:
            anchors[id(p)] = p
            scaled.add(v.vid)
            continue
        if p.kind not in COVARIANT:
            why.append(f"%{v.vid} is {p.kind.value}, which a scale cannot cross")
            continue
        scaled.add(v.vid)
        stack += list(p.inputs)
    return scaled, anchors, why


def plan(graph: Graph, factor: float = LOG2_E) -> tuple[list, dict, set, list[str]]:
    """Decide whether `factor` can be absorbed, without changing anything.

    Returns `(folds, anchors, scaled, reasons)`. `folds` are the `(mul, src)`
    pairs to delete, `anchors` the constant multiplies whose constant grows by
    `factor`, and `reasons` is why it was refused -- empty means go ahead.
    """
    readers = _readers(graph)
    outputs = {v.vid for v in graph.outputs}
    folds: list[tuple[Op, Value]] = []
    anchors: dict[int, Op] = {}
    scaled: set[int] = set()
    why: list[str] = []

    for op in graph.ops:
        if op.kind is not OpKind.MUL:
            continue
        pair = _const_operand(graph, op)
        if pair is None or abs(pair[1].attrs["value"] - factor) > 1e-12:
            continue
        rest = [v for v in op.inputs if v.vid != pair[0].vid]
        if len(rest) != 1:
            continue
        if not any(r.kind is OpKind.EXP2 for r in readers.get(op.out.vid, [])):
            continue
        if op.out.vid in outputs:
            why.append(f"%{op.out.vid} is a graph output")
            continue
        cone, anch, bad = _cone(graph, rest[0])
        if bad:
            why += bad
            continue
        folds.append((op, rest[0]))
        anchors.update(anch)
        scaled |= cone

    if not folds:
        return [], {}, set(), why or ["no `* log2 e` feeds an exp2"]

    values = {_const_operand(graph, a)[1].attrs["value"] for a in anchors.values()}
    if len(values) != 1:
        why.append(f"the cone is scaled by {len(values)} different constants")

    inside = {id(o) for o in anchors.values()} | {id(m) for m, _ in folds}
    for vid in sorted(scaled):
        if vid in outputs:
            why.append(f"%{vid} is a graph output and scaling it changes the result")
        for r in readers.get(vid, []):
            if r.out.vid not in scaled and id(r) not in inside:
                why.append(f"%{vid} escapes into {r.kind.value} %{r.out.vid}")
    return (folds, anchors, scaled, why) if not why else ([], {}, set(), why)


def absorb(graph: Graph, factor: float = LOG2_E) -> int:
    """Fold `factor` into the scale above every `exp2` that pays for it.

    Mutates `graph` and returns how many multiplies it deleted; zero means the
    kernel did not qualify and nothing changed. Idempotent: a second call finds
    no `* factor` left to absorb.
    """
    folds, anchors, _scaled, why = plan(graph, factor)
    if why:
        return 0

    readers = _readers(graph)
    for anchor in anchors.values():
        cv, cop = _const_operand(graph, anchor)
        fresh = graph.const(cop.attrs["value"] * factor, cv.dtype)
        # A constant has no operands, so the front of the list is always a
        # legal position and an in-order evaluator stays happy.
        graph.ops.insert(0, graph.ops.pop())
        anchor.inputs = tuple(fresh if v.vid == cv.vid else v for v in anchor.inputs)

    for mul, src in folds:
        for r in readers.get(mul.out.vid, []):
            r.inputs = tuple(src if v.vid == mul.out.vid else v for v in r.inputs)
        graph.ops.remove(mul)
        del graph._producer[mul.out.vid]
    return len(folds)
