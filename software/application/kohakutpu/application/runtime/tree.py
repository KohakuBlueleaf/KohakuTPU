"""A reduction down the rows (over an outer axis): ``out[c] = post(op over r of
e(row r)[c])`` as a halving tree of generated rows kernels. Each level folds
the top half of the rows onto the bottom half (the first level computing `e`
on both halves on the fly); an odd row left over waits beside the tree and
folds in at the end, where `post` (the expression around the reduction)
applies too.

Levels: ceil(log2 R) kernels plus one, every intermediate an f16 [rows, W]
scratch the device frees after the last level.
"""

from dataclasses import replace

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import rows as RW

OP = {"sum": "add", "max": "max", "prod": "mul"}


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
    item = 2
    runs, scratch, pending = [], [], []
    try:
        n, inner, ops, bnd = R, red.arg, list(operands), dict(binds)
        cur = None  # once the first level ran: the scratch rows
        while n > 1:
            h = n // 2
            if cur is None:
                k = len(ops)
                expr = X.Op(op, (inner, _renumber(inner, k)))
                shifted = [_shift(o, h, W) for o in ops]
                lvl_ops = ops + shifted
                lvl_b = {
                    **{f"in{i}": bnd[f"in{i}"] for i in range(k)},
                    **{f"in{i + k}": bnd[f"in{i}"] for i in range(k)},
                }
                if n % 2:
                    # The odd last row of `e`, kept for the end.
                    last = dev.alloc((4 * W) * item)
                    scratch.append(last)
                    runs += RW.run(
                        dev,
                        inner,
                        [_shift(o, n - 1, W) for o in ops],
                        bnd,
                        1,
                        W,
                        Operand("full", pitch=W),
                        last,
                        info,
                    )
                    pending.append(last)
            else:
                expr = X.Op(op, (X.Load(0), X.Load(1)))
                lvl_ops = [
                    Operand("full", pitch=W, rows=n),
                    Operand("full", pitch=W, offset=h * W, rows=n),
                ]
                lvl_b = {"in0": cur, "in1": cur}
                if n % 2:
                    pending.append(cur + (n - 1) * W * item)
            nxt = dev.alloc((h + 4) * W * item)
            scratch.append(nxt)
            runs += RW.run(
                dev, expr, lvl_ops, lvl_b, h, W, Operand("full", pitch=W), nxt, info
            )
            cur, n = nxt, h
        # The last row: the tree's (or e's own, for one row), the odd rows, post.
        if cur is None:
            head, head_ops, head_b = (
                inner,
                list(ops),
                {f"in{i}": bnd[f"in{i}"] for i in range(len(ops))},
            )
        else:
            head, head_ops, head_b = (
                X.Load(0),
                [Operand("full", pitch=W, rows=1)],
                {"in0": cur},
            )
        acc = head
        for p in pending:
            j = len(head_ops)
            head_ops.append(Operand("full", pitch=W, rows=1))
            head_b[f"in{j}"] = p
            acc = X.Op(op, (acc, X.Load(j)))
        base = len(head_ops)
        final = _place(post, acc, base)
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


__all__ = ["OP", "column"]
