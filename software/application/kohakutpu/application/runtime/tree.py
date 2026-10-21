"""A reduction down the rows (over an outer axis): ``out[c] = post(op over r of
e(row r)[c])`` as a halving tree of generated rows kernels. Each level folds
the top half of the rows onto the bottom half; an odd row left over is moved
into the next level's scratch as its last row. The first level computes `e`
on both halves on the fly while its reads, twice over, fit a core's memory
views; else `e` is computed into a scratch first. The last kernel applies
`post` (the expression around the reduction) to the one row left.

Levels: ceil(log2 R) kernels (one more when `e` is computed first), every
intermediate an f16 [rows, W] scratch freed after the last level.
"""

from dataclasses import replace

from kohakutpu.application.kernels import convert as C
from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime import rows as RW

OP = {"sum": "add", "max": "max", "prod": "mul"}
#: Memory views one vector core's kernel takes (`l2/vector.py` DESCS).
VIEWS = 8


def _shift(o: Operand, rows: int, W: int) -> Operand:
    """`o` read `rows` rows further down."""
    if o.kind in ("full", "tile"):
        return replace(o, offset=o.offset + rows * (o.pitch or W))
    if o.kind == "row":
        return replace(o, offset=o.offset + rows)
    return o


def _renumber(e, by: int):
    if isinstance(e, X.Load):
        return X.Load(e.i + by)
    if isinstance(e, X.Op):
        return X.Op(e.name, tuple(_renumber(a, by) for a in e.args))
    if isinstance(e, X.Reduce):
        return X.Reduce(e.op, _renumber(e.arg, by))
    return e


def column(
    dev, e, operands, binds: dict, R: int, W: int, out: Operand, out_at: int, info=None
) -> list:
    """`e` holds one `Reduce` (its whole arg read at row r, the rest of `e`
    reads no row operand); the result row goes to `out` at `out_at`."""
    reds = X.reduces(e)
    if len(reds) != 1:
        raise RW.RowsError("a reduction down the rows holds one reduction")
    red = reds[0]
    op = OP[red.op]
    post, keep = RW.substitute(e, red, X.Load(0))
    row = 2 * W  # bytes a scratch row
    full = Operand("full", pitch=W)
    runs: list = []
    scratch: list = []

    def alloc(rows: int) -> int:
        at = dev.alloc(rows * row)
        scratch.append(at)
        return at

    try:
        n, ops, bnd = R, list(operands), dict(binds)
        cur = None  # once a level ran: the scratch rows
        if n > 1 and 2 * len(ops) + 1 > VIEWS:
            # e's reads twice over pass a core's views: e into a scratch first.
            cur = alloc(n)
            runs += RW.run(dev, red.arg, ops, bnd, n, W, full, cur, info)
        while n > 1:
            h = n // 2
            nxt = alloc(h + n % 2)
            if cur is None:
                k = len(ops)
                expr = X.Op(op, (red.arg, _renumber(red.arg, k)))
                lvl_ops = ops + [_shift(o, h, W) for o in ops]
                lvl_b = {
                    **{f"in{i}": bnd[f"in{i}"] for i in range(k)},
                    **{f"in{i + k}": bnd[f"in{i}"] for i in range(k)},
                }
                if n % 2:
                    # e's odd last row: the next level's last.
                    last = [_shift(o, n - 1, W) for o in ops]
                    runs += RW.run(
                        dev, red.arg, last, bnd, 1, W, full, nxt + h * row, info
                    )
            else:
                expr = X.Op(op, (X.Load(0), X.Load(1)))
                lvl_ops = [
                    Operand("full", pitch=W, rows=n),
                    Operand("full", pitch=W, offset=h * W, rows=n),
                ]
                lvl_b = {"in0": cur, "in1": cur}
                if n % 2:
                    k, given = C.copy_kernel(row)
                    at = {"src": cur + (n - 1) * row, "dst": nxt + h * row}
                    runs.append(FM.launch(dev, k, given, at, kind="copy"))
            runs += RW.run(dev, expr, lvl_ops, lvl_b, h, W, full, nxt, info)
            cur, n = nxt, h + n % 2
        # The one row left (e's own, for one row), under post.
        if cur is None:
            head = red.arg
            head_ops = list(ops)
            head_b = {f"in{i}": bnd[f"in{i}"] for i in range(len(ops))}
        else:
            head = X.Load(0)
            head_ops = [Operand("full", pitch=W, rows=1)]
            head_b = {"in0": cur}
        base = len(head_ops)
        final = _place(post, head, base)
        for j, i in enumerate(keep):
            head_ops.append(operands[i])
            head_b[f"in{base + j}"] = binds[f"in{i}"]
        runs += RW.run(dev, final, head_ops, head_b, 1, W, out, out_at, info)
    finally:
        for a in scratch:
            dev.free(a)
    return runs


def _place(post, acc, base: int):
    """`post` (its `Load(0)` the reduced row, `Load(j)` its j-th other read)
    with the row as `acc` and the other reads after the `base` head ones."""
    if isinstance(post, X.Load):
        return acc if post.i == 0 else X.Load(base + post.i - 1)
    if isinstance(post, X.Op):
        return X.Op(post.name, tuple(_place(a, acc, base) for a in post.args))
    return post


__all__ = ["OP", "VIEWS", "column"]
