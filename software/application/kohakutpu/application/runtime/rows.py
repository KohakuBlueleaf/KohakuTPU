"""Running a generated rows kernel: `e` over [R, W] into an [R, W] view or a
row statistic. A reduction across a row wider than one column block runs
as per-block partials, then folds them (recursively) under the expression
around the reduction; any other reduction across such a row is computed
first, as a statistic the rest reads."""

from dataclasses import replace

import numpy as np

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import forms as FM


class RowsError(RuntimeError):
    """A rows computation the generated kernels do not cover, named."""


def _launch(dev, e, operands, binds, R, W, out, info, cores):
    """Generate, compile and run the rows kernel (the retile pass fits its
    row tile to the core's registers and L1)."""
    operands, binds = _based(operands, binds, "in")
    if "out" in binds:
        (out,), binds = _based([out], binds, "out")
    k, given, nblk = V.rows_kernel(e, tuple(operands), R, W, out, cores)
    return FM.launch(dev, k, given, binds, info=info), given, nblk


def _based(operands, binds: dict, port: str) -> tuple:
    """Each operand's offset moved into its bound address where whole words
    (the rows it holds past it kept): a kernel of one shape at any offset is
    one compile. `port` "in" binds operand i as ``in{i}``, else one."""
    binds, out = dict(binds), []
    for i, o in enumerate(operands):
        key = f"in{i}" if port == "in" else port
        item = 4 if o.dtype == "f32" else 2
        if o.kind in ("row", "scalar"):
            shift, rows = 64 * item // 2 * o.offset, max(o.rows - o.offset, 0)
        else:
            shift = item * o.offset
            rows = max(o.rows - o.offset // o.pitch, 0) if o.pitch else o.rows
        if not o.offset or shift % 32 or key not in binds:
            out.append(o)
            continue
        binds[key] += shift
        out.append(replace(o, offset=0, rows=rows))
    return out, binds


def run(
    dev, e, operands, binds: dict, R: int, W: int, out: Operand, out_at: int, info=None
) -> list:
    """Compute `e` (`Load(i)` reading ``binds["in{i}"]`` as `operands[i]`)
    into `out` at `out_at`; returns the runs."""
    _whole_rows(operands, W)
    info = dict(info or {})
    cores = dev.machine.count("VC")
    reds = _reductions(e)
    if W > V.MAX_BLOCK and reds and (out.kind != "row" or len(reds) > 1):
        # A reduction across column blocks, not the one result: its
        # statistic first (an innermost one), read back as a row operand.
        return _stat_first(dev, e, operands, binds, R, W, out, out_at, info)
    if out.kind != "row":
        one, _, _ = _launch(
            dev, e, operands, dict(binds, out=out_at), R, W, out, info, cores
        )
        return [one]
    if W <= V.MAX_BLOCK:
        one, _, _ = _launch(
            dev, e, operands, dict(binds, out=out_at), R, W, Operand("row"), info, cores
        )
        return [one]
    red = reds[0]
    part = Operand("row")
    _, given, nblk = V.rows_kernel(red, tuple(operands), R, W, part, cores)
    nbytes = given["out"].nbytes()
    parts = [dev.alloc(nbytes), dev.alloc(nbytes)]
    try:
        one, _, _ = _launch(
            dev,
            red,
            operands,
            dict(binds, out=parts[0]),
            R,
            W,
            part,
            dict(info, blocks=nblk),
            cores,
        )
        runs = [one]
        # Each block's partial fills its 32 columns: blocks halved (first
        # half with last, an odd middle one carried) until one is left.
        pitch, width = 32 * nblk, nblk
        both = X.op(FOLD[red.op], X.Load(0), X.Load(1))
        while width > 1:
            half = width // 2
            src, dst = parts
            ops = [
                Operand("full", pitch=pitch, rows=R),
                Operand("full", pitch=pitch, rows=R, offset=32 * (width - half)),
            ]
            if width - half > half:
                # The middle block, carried over to the next round.
                mid = Operand("full", pitch=pitch, rows=R, offset=32 * half)
                runs += run(
                    dev,
                    X.Load(0),
                    [mid],
                    {"in0": src},
                    R,
                    32,
                    Operand("full", pitch=pitch),
                    dst + 64 * half,
                    info,
                )
            runs += run(
                dev,
                both,
                ops,
                {"in0": src, "in1": src},
                R,
                32 * half,
                Operand("full", pitch=pitch),
                dst,
                info,
            )
            parts.reverse()
            width -= half
        post, keep = substitute(e, red, _held())
        ops2 = [Operand("full", pitch=pitch, rows=R)]
        b2 = {"in0": parts[0]}
        for j, i in enumerate(keep, start=1):
            if operands[i].kind not in ("row", "scalar"):
                raise RowsError(
                    "a row-wide operand outside a reduction whose result is a statistic"
                )
            ops2.append(operands[i])
            b2[f"in{j}"] = binds[f"in{i}"]
        runs += run(dev, post, ops2, b2, R, 32, out, out_at, info)
    finally:
        for p in parts:
            dev.free(p)
    return runs


#: How two partials of a reduction combine.
FOLD = {"sum": "add", "max": "max", "prod": "mul"}


def _reductions(e) -> list:
    """Every reduction in `e`, nested ones too, a shared node once."""
    out: list = []

    def walk(x):
        if isinstance(x, X.Reduce):
            if any(r is x for r in out):
                return
            out.append(x)
            walk(x.arg)
        elif isinstance(x, X.Op):
            for a in x.args:
                walk(a)

    walk(e)
    return out


def _held():
    """A statistic's value from its 32 equal columns (Load 0): their max."""
    return X.Reduce("max", X.Load(0))


def _swap(e, red, new):
    """`e` with the node `red` replaced by `new`, every read kept (a node
    the expression shares stays one)."""
    memo: dict = {}

    def walk(x):
        if id(x) not in memo:
            if x is red:
                memo[id(x)] = new
            elif isinstance(x, X.Op):
                memo[id(x)] = X.Op(x.name, tuple(walk(a) for a in x.args))
            elif isinstance(x, X.Reduce):
                memo[id(x)] = X.Reduce(x.op, walk(x.arg))
            else:
                memo[id(x)] = x
        return memo[id(x)]

    return walk(e)


def _stat_first(dev, e, operands, binds, R: int, W: int, out, out_at: int, info):
    """`e` with an innermost reduction computed first into a statistic and
    read back as a row operand (`run` again: one reduction fewer)."""
    leaf = next(r for r in _reductions(e) if not _reductions(r.arg))
    inner, used = substitute(leaf, None, None, first=0)
    ops = [operands[i] for i in used]
    held = Operand("row", rows=R)
    stat = dev.alloc(stat_bytes(R))
    try:
        b1 = {f"in{j}": binds[f"in{i}"] for j, i in enumerate(used)}
        runs = run(dev, inner, ops, b1, R, W, held, stat, info)
        n = len(operands)
        post = _swap(e, leaf, X.Load(n))
        b2 = dict(binds, **{f"in{n}": stat})
        ops2 = [*operands, held]
        runs += run(dev, post, ops2, b2, R, W, out, out_at, info)
    finally:
        dev.free(stat)
    return runs


def _whole_rows(operands, W: int) -> None:
    """Every row a kernel reads is whole 32-byte words inside its pitch (the
    runner lays any other read out first, `run._bind`)."""
    for o in operands:
        if o.kind == "full" and (
            o.offset % 16 or o.pitch % 16 or o.offset % o.pitch + W > o.pitch
        ):
            raise ValueError(f"a rows kernel read off whole rows of {W}: {o}")
        if o.kind == "col" and o.offset % 16:
            raise ValueError(f"a rows kernel row read off a word: {o}")


def substitute(e, red, fold, first: int = 1):
    """`e` with `red` replaced by `fold` (over Load 0) and the reads left
    outside it renumbered from `first`: ``(expression, original read
    indices)``."""
    keep, mapping = [], {}

    def walk(x):
        if x is red:
            return fold
        if isinstance(x, X.Load):
            if x.i not in mapping:
                mapping[x.i] = first + len(keep)
                keep.append(x.i)
            return X.Load(mapping[x.i])
        if isinstance(x, X.Op):
            return X.Op(x.name, tuple(walk(a) for a in x.args))
        if isinstance(x, X.Reduce):
            return X.Reduce(x.op, walk(x.arg))
        return x

    return walk(e), keep


def stat_bytes(R: int) -> int:
    """Bytes of an [R, 32] statistic form, rows rounded to whole trips."""
    return -(-R // 16) * 16 * 64


def rows_of(shape) -> int:
    return int(np.prod(shape[:-1])) if len(shape) > 1 else 1


__all__ = ["RowsError", "rows_of", "run", "stat_bytes", "substitute"]
