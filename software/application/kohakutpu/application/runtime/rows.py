"""Running a generated rows kernel: `e` over [R, W] into an [R, W] view or a
row statistic. A reduction across a row wider than one column block runs
as per-block partials, then folds them (recursively) under the expression
around the reduction."""

import numpy as np

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import forms as FM
from kohakutpu.language.text.syntax import TextError


class RowsError(RuntimeError):
    """A rows computation the generated kernels do not cover, named."""


#: Row bands a trip, tried in order while the vector registers overflow.
BANDS = (None, 2, 1)


def _launch(dev, e, operands, binds, R, W, out, info, cores):
    """Generate, compile and run the rows kernel, fewer bands a trip while
    the L2 compiler finds more values than the core's 32 registers hold."""
    for bands in BANDS:
        try:
            k, given, nblk = V.rows_kernel(e, tuple(operands), R, W, out, cores, bands)
            return FM.launch(dev, k, given, binds, info=info), given, nblk
        except TextError as err:
            if "vector registers" not in str(err) or bands == BANDS[-1]:
                raise RowsError(f"rows of {W}: {err}") from None
    raise AssertionError("unreachable")


def run(
    dev, e, operands, binds: dict, R: int, W: int, out: Operand, out_at: int, info=None
) -> list:
    """Compute `e` (`Load(i)` reading ``binds["in{i}"]`` as `operands[i]`)
    into `out` at `out_at`; returns the runs."""
    info = dict(info or {})
    cores = dev.machine.count("VC")
    if out.kind != "row":
        spill = V.overrun(R) * (out.pitch or W) * 2
        if spill > D.TAIL:
            raise RowsError(
                f"{R} rows of {W}: the last band writes {spill} B past the buffer"
            )
        one, _, _ = _launch(
            dev, e, operands, dict(binds, out=out_at), R, W, out, info, cores
        )
        return [one]
    if W <= V.MAX_BLOCK:
        one, _, _ = _launch(
            dev, e, operands, dict(binds, out=out_at), R, W, Operand("row"), info, cores
        )
        return [one]
    reds = X.reduces(e)
    if len(reds) != 1 or reds[0].op == "prod":
        raise RowsError(
            f"a product, or several reductions, across a row of {W} (wider than {V.MAX_BLOCK})"
        )
    red = reds[0]
    _, given, nblk = V.rows_kernel(red, tuple(operands), R, W, Operand("row"), cores)
    part = dev.alloc(given["out"].nbytes())
    try:
        one, _, _ = _launch(
            dev,
            red,
            operands,
            dict(binds, out=part),
            R,
            W,
            Operand("row"),
            dict(info, blocks=nblk),
            cores,
        )
        runs = [one]
        fold = X.Reduce(red.op, X.Load(0))
        if red.op == "sum":
            fold = X.op("mul", fold, 1.0 / 32)  # each partial is held 32 times
        post, keep = substitute(e, red, fold)
        ops2 = [Operand("full", pitch=32 * nblk, rows=R)]
        b2 = {"in0": part}
        for j, i in enumerate(keep, start=1):
            if operands[i].kind not in ("row", "scalar"):
                raise RowsError(
                    "a row-wide operand outside a reduction whose result is a statistic"
                )
            ops2.append(operands[i])
            b2[f"in{j}"] = binds[f"in{i}"]
        runs += run(dev, post, ops2, b2, R, 32 * nblk, out, out_at, info)
    finally:
        dev.free(part)
    return runs


def substitute(e, red, fold):
    """`e` with `red` replaced by `fold` (over Load 0) and the reads left
    outside it renumbered from 1: ``(expression, original read indices)``."""
    keep, mapping = [], {}

    def walk(x):
        if x is red:
            return fold
        if isinstance(x, X.Load):
            if x.i not in mapping:
                keep.append(x.i)
                mapping[x.i] = len(keep)
            return X.Load(mapping[x.i])
        if isinstance(x, X.Op):
            return X.Op(x.name, tuple(walk(a) for a in x.args))
        return x

    return walk(e), keep


def stat_bytes(R: int) -> int:
    """Bytes of an [R, 32] statistic form, rows rounded to whole trips."""
    return -(-R // 16) * 16 * 64


def rows_of(shape) -> int:
    return int(np.prod(shape[:-1])) if len(shape) > 1 else 1


__all__ = ["RowsError", "rows_of", "run", "stat_bytes", "substitute"]
