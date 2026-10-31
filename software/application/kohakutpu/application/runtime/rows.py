"""Running a generated rows kernel: `e` over [R, W] into an [R, W] view or a
row statistic (R f16 values). A reduction across a row wider than one column
block runs as per-block partials, then folds them (statistics alone) under
the expression around the reduction; any other reduction across such a row
is computed first, as a statistic the rest reads."""

import json
import os
from dataclasses import replace

import numpy as np

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.classes.policy import ShapeClass
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime import launch_reductions as LR
from kohakutpu.compiler import _rs

#: Whether a multi-stage AST (wide row reduction or column tree reduction) is
#: compiled into a single kernel package rather than split across launches;
#: the default is the native planner's (`ktpu_kernels::gen::reductions::
#: defaults`).
SINGLE_PACKAGE_AST = json.loads(_rs.ROWS_DEFAULTS)["single_package_ast"]
#: Optional mutant mode for witness testing ("drop_member", "wrong_order", "alias_members").
MUTANT: str | None = None


def single_package_enabled() -> bool:
    return (
        os.environ.get("KTPU_SINGLE_PACKAGE_AST", "1" if SINGLE_PACKAGE_AST else "0")
        == "1"
    )


class RowsError(RuntimeError):
    """A rows computation the generated kernels do not cover, named."""


def _launch(dev, e, operands, binds, R, W, out, info, cores):
    """Generate, compile and run the rows kernel (the retile pass fits its
    row tile to the core's registers and L1)."""
    operands, binds = _based(operands, binds, "in")
    if "out" in binds:
        (out,), binds = _based([out], binds, "out")
    if os.environ.get("KTPU_CLASS_ROWS_ROUTE", "0") == "1" and (
        os.environ.get("KTPU_CLASS_ROWS_DIRECT", "0") == "1"
        and 0 < W <= V.MAX_BLOCK
        and W % 32 == 0
        and W & (W - 1) == 0
        and out.dtype == "f16"
        and all(o.dtype == "f16" and o.kind != "tile" for o in operands)
        and V.class_rows.bands_fit(operands, out, W)
        >= (V.STAT_ROWS // 4 if V.holds_stat(operands, out.kind) else 1)
    ):
        kernel, given, nblk = V.rows_kernel(
            e,
            tuple(operands),
            R,
            W,
            out,
            cores,
            shape_class=ShapeClass(32, V.MAX_BLOCK),
        )
        disjoint = all(
            binds[f"in{i}"] + given[f"in{i}"].nbytes() <= binds["out"]
            or binds["out"] + given["out"].nbytes() <= binds[f"in{i}"]
            for i in range(len(operands))
        )
        if kernel.class_info["direct"] and disjoint:
            return FM.launch(dev, kernel, given, binds, info=info), given, nblk
    _, given, nblk = V.rows_kernel(e, tuple(operands), R, W, out, cores)
    run = FM.generated.launch(
        dev,
        "vector.rows_kernel",
        {
            "e": FM.generated.expr(e),
            "operands": [vars(o) for o in operands],
            "R": R,
            "W": W,
            "out": vars(out),
        },
        binds,
        info=info,
    )
    return run, given, nblk


def _prepared(dev, e, operands, binds, R, W, out, out_at, info):
    """One launch of `e` with each bound `forms.Pending` read composed or made
    inside it (the generator `prepared.rows_kernel`); None when neither runs
    (nothing run)."""
    made, steps, alias, first = {}, {}, {}, {}
    for i in range(len(operands)):
        b = binds.get(f"in{i}")
        if not isinstance(b, FM.Pending):
            continue
        if id(b) in first:
            # The same buffer read again (another row range): made once.
            alias[i] = first[id(b)]
            continue
        plans = FM.plans_of(dev, b.steps)
        if plans is None and FM.COMPOSED == "off":
            return None
        made[i], steps[i], first[id(b)] = plans, b.steps, i
    plain = {k: v for k, v in binds.items() if not isinstance(v, FM.Pending)}
    ops, plain = _based(list(operands), plain, "in")
    ops = [operands[i] if i in made or i in alias else o for i, o in enumerate(ops)]
    arguments = {
        "e": FM.generated.expr(e),
        "operands": [vars(o) for o in ops],
        "R": R,
        "W": W,
        "out": vars(out),
        "made": {str(i): [*map(FM.made_by, s)] for i, s in steps.items()},
        "alias": {str(i): j for i, j in alias.items()},
    }
    read = dict(arguments, composed=sorted(made))
    planned = all(p is not None for p in made.values())
    launches = (
        [{"gen": "prepared.rows_kernel", "args": arguments}]
        if planned
        else [FM.relayout_call(s) for ss in steps.values() for s in ss]
    )
    plain["out"] = out_at
    info = dict(info, prepared=sorted(made))
    if FM.composed(dev, {"gen": "prepared.rows_kernel", "args": read}, launches):
        plain |= {f"x{i}": ss[0].src for i, ss in steps.items()}
        return FM.generated.launch(dev, "prepared.rows_kernel", read, plain, info=info)
    if not planned:
        return None
    for i, plans in made.items():
        plain[f"x{i}"] = steps[i][0].src
        plain |= FM.step_binds(dev, plans, f"p{i}_")
    return FM.generated.launch(dev, "prepared.rows_kernel", arguments, plain, info=info)


def _based(operands, binds: dict, port: str) -> tuple:
    """Rebase whole rows of pitched views, whole words of scalar views.

    A pitched port declares complete rows. Its column offset must stay in the
    kernel; moving that offset into the binding would extend the declared last
    row beyond its allocation. `port` "in" binds operand i as ``in{i}``.
    """
    binds, out = dict(binds), []
    for i, o in enumerate(operands):
        key = f"in{i}" if port == "in" else port
        item = 4 if o.dtype == "f32" else 2
        if o.kind in ("full", "tile"):
            moved = o.offset // o.pitch * o.pitch
        elif o.kind == "scalar":
            moved = o.offset // 16 * 16
        else:
            moved = o.offset
        shift = item * moved
        if o.kind in ("row", "scalar"):
            rows = max(o.rows - moved, 0)
        else:
            rows = max(o.rows - o.offset // o.pitch, 0) if o.pitch else o.rows
        if not moved or shift % 32 or key not in binds:
            out.append(o)
            continue
        binds[key] += shift
        out.append(replace(o, offset=o.offset - moved, rows=rows))
    return out, binds


def _blocks(e, operands, rows, width, cores):
    return V.rows_kernel(e, tuple(operands), rows, width, Operand("row"), cores)[2]


def run(
    dev, e, operands, binds: dict, R: int, W: int, out: Operand, out_at: int, info=None
) -> list:
    """Compute `e` (`Load(i)` reading ``binds["in{i}"]`` as `operands[i]`)
    into `out` at `out_at`; returns the runs. A bound `forms.Pending` is
    made inside the kernel when it is one launch (`prepared.rows_kernel`),
    else by its own relayouts first."""
    _whole_rows(operands, W)
    info = dict(info or {})
    cores = dev.machine.count("VC")
    reds = _reductions(e)
    blocks = 1
    if reds:
        blocks = _blocks(reds[0], operands, R, W, cores)
    pend = {k: v for k, v in binds.items() if isinstance(v, FM.Pending)}
    # A reduction's partials across column blocks: one launch reads the
    # operands (then folds of partials), so it makes the pending ones.
    partials = blocks > 1 and out.kind == "row" and len(reds) == 1
    if pend:
        if FM.PREPARED and blocks == 1:
            got = _prepared(dev, e, operands, binds, R, W, out, out_at, info)
            if got is not None:
                return [got]
        if not (FM.PREPARED and partials):
            binds = {k: (v.made().addr if k in pend else v) for k, v in binds.items()}
    if blocks > 1 and reds and (out.kind != "row" or len(reds) > 1):
        # A reduction across column blocks, not the one result: its
        # statistic first (an innermost one), read back as a row operand.
        return _stat_first(dev, e, operands, binds, R, W, out, out_at, info)
    if out.kind != "row":
        one, _, _ = _launch(
            dev, e, operands, dict(binds, out=out_at), R, W, out, info, cores
        )
        return [one]
    if blocks == 1:
        one, _, _ = _launch(
            dev, e, operands, dict(binds, out=out_at), R, W, Operand("row"), info, cores
        )
        return [one]
    red = reds[0]
    part = Operand("row")
    if single_package_enabled():
        binds = {
            k: v.made().addr if isinstance(v, FM.Pending) else v
            for k, v in binds.items()
        }
        _, keep = substitute(e, red, X.Load(0))
        for i in keep:
            if operands[i].kind not in ("row", "scalar"):
                raise RowsError(
                    "a row-wide operand outside a reduction whose result is a statistic"
                )
        one = _launch_wide(
            dev,
            e,
            operands,
            dict(binds, out=out_at),
            R,
            W,
            out,
            dict(info, blocks=blocks),
        )
        return [one]
    _, given, nblk = V.rows_kernel(red, tuple(operands), R, W, part, cores)
    rq = given["out"].shape[-1]  # a block's partials, whole words
    nbytes = given["out"].nbytes()
    parts = [dev.alloc(nbytes), dev.alloc(nbytes)]
    try:
        info1 = dict(info, blocks=nblk)
        one = None
        if pend:
            one = _prepared(dev, red, operands, binds, R, W, part, parts[0], info1)
            if one is None:
                binds = {
                    k: (v.made().addr if k in pend else v) for k, v in binds.items()
                }
        if one is None:
            one, _, _ = _launch(
                dev, red, operands, dict(binds, out=parts[0]), R, W, part, info1, cores
            )
        runs = [one]
        # Blocks rq values apart, halved (an odd middle one carried) to one.
        width = nblk
        both = X.op(FOLD[red.op], X.Load(0), X.Load(1))
        while width > 1:
            half = width // 2
            src, dst = parts
            n = half * rq
            ops = [
                Operand("row", rows=n),
                Operand("row", rows=n, offset=(width - half) * rq),
            ]
            runs += run(
                dev, both, ops, {"in0": src, "in1": src}, n, 32, part, dst, info
            )
            if width - half > half:
                at = {"src": src + 2 * half * rq, "dst": dst + 2 * half * rq}
                runs.append(FM.generated.copy(dev, 2 * rq, at))
            parts.reverse()
            width -= half
        post, keep = substitute(e, red, X.Load(0))
        ops2 = [Operand("row", rows=R)]
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
    """Bytes of R statistic values in whole words (a rows kernel writes
    them whole)."""
    return V.stat_words(R) * 32


def rows_of(shape) -> int:
    return int(np.prod(shape[:-1])) if len(shape) > 1 else 1


def _launch_wide(dev, e, operands, binds, R, W, out, info):
    operands, binds = _based(operands, binds, "in")
    if "out" in binds:
        (out,), binds = _based([out], binds, "out")
    return FM.generated.launch(
        dev,
        "reductions.wide_fold_kernel",
        {
            "e": FM.generated.expr(e),
            "operands": [vars(o) for o in operands],
            "R": R,
            "W": W,
            "out": vars(out),
        },
        binds,
        info=info,
    )


__all__ = [
    "MUTANT",
    "SINGLE_PACKAGE_AST",
    "RowsError",
    "emit_rows_stages",
    "rows_of",
    "run",
    "single_package_enabled",
    "stat_bytes",
    "substitute",
    "wide_fold_kernel",
]


def emit_rows_stages(*args, **kwargs):
    return LR.emit_rows_stages(*args, **kwargs)


def wide_fold_kernel(*args, **kwargs):
    return LR.wide_fold_kernel(*args, **kwargs)
