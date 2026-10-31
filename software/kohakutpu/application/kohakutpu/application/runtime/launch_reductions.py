"""Single-package reduction emitters retaining each f16 partial-store boundary."""

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import rows as RW
from kohakutpu.language import dsl as kt


def emit_rows_stages(
    bufs_map, e, operands, R: int, W: int, out: Operand, cores: int
) -> None:
    """Emit all full and tail row stages of `e` over `[R, W]` into the active
    DSL kernel trace."""
    operands = tuple(operands)
    q = V.STAT_ROWS if V.holds_stat(operands, out.kind) else 4
    pl = V.plan(operands, out.kind, R, W, None, quantum=q)
    for sp, row0, rows in V.stages(pl, R, q):
        if rows is None:
            V.rows_stage(bufs_map, e, operands, out, sp, cores, row0=row0)
        else:
            V._tail_band(bufs_map, e, operands, out, sp, row0, rows, W)


def wide_fold_kernel(e, operands, R: int, W: int, out: Operand, cores: int):
    """Single-package wide row reduction kernel: computes per-block partials,
    halves them across blocks in internal scratch, and applies `post`."""
    operands = tuple(operands)
    reds = RW._reductions(e)
    red = reds[0]
    fold_op = RW.FOLD[red.op]
    post, keep = RW.substitute(e, red, X.Load(0))
    kkey = (
        "rows_wide_fold",
        V.expr_key(e),
        operands,
        R,
        W,
        out,
        cores,
        fold_op,
        V.expr_key(post),
        tuple(keep),
        RW.MUTANT,
        kt.Kernel.build,
    )
    hit = V._kernels.get(kkey)
    if hit is not None:
        V._kernels.move_to_end(kkey)
        return hit
    part = Operand("row")
    pl = V.plan(operands, "row", R, W, None, quantum=V.STAT_ROWS)
    nblk = pl.nblk
    rq = max(part.rows, V.stat_words(R) * V.STAT_ROWS)
    ports, given = {}, {}
    for i, o in enumerate(operands):
        ports[f"in{i}"], given[f"in{i}"] = V.port(o, R, W)
    if out.kind == "row":
        out_rows = max(out.rows, V.stat_words(R) * V.STAT_ROWS)
        out_shape = (out_rows,)
    else:
        out_shape = V.port(out, R, 32)[1].shape
    ports["out"] = kt.Out(*out_shape)
    given["out"] = kt.TensorSpec.make(out_shape)

    def body(**p):
        s0_2d = kt.buffer((nblk, rq), "f16")
        s0 = kt.buffer((nblk * rq,), "f16")
        s1 = kt.buffer((nblk * rq,), "f16")
        if RW.MUTANT != "wrong_order":
            emit_rows_stages(dict(p, out=s0_2d), red, operands, R, W, part, cores)
            kt.copy(s0, s0_2d)
        cur_src, cur_dst = s0, s1
        width = nblk
        both = X.op(fold_op, X.Load(0), X.Load(1))
        while width > 1:
            half = width // 2
            n = half * rq
            ops = (
                Operand("row", rows=n),
                Operand(
                    "row",
                    rows=n,
                    offset=(0 if RW.MUTANT == "alias_members" else width - half) * rq,
                ),
            )
            if RW.MUTANT == "drop_member" and width == nblk:
                cur_src, cur_dst = cur_dst, cur_src
                width -= half
                continue
            emit_rows_stages(
                {"in0": cur_src, "in1": cur_src, "out": cur_dst},
                both,
                ops,
                n,
                32,
                part,
                cores,
            )
            if width - half > half:
                off_el = half * rq
                kt.copy(
                    cur_dst[off_el : off_el + rq],
                    cur_src[off_el : off_el + rq],
                )
            cur_src, cur_dst = cur_dst, cur_src
            width -= half
        if RW.MUTANT == "wrong_order":
            emit_rows_stages(dict(p, out=s0_2d), red, operands, R, W, part, cores)
            kt.copy(s0, s0_2d)
        ops2 = [Operand("row", rows=R)]
        b2 = {"in0": cur_src, "out": p["out"]}
        for j, i in enumerate(keep, start=1):
            ops2.append(operands[i])
            b2[f"in{j}"] = p[f"in{i}"]
        emit_rows_stages(b2, post, tuple(ops2), R, 32, out, cores)

    key = V.name(
        "rows", "wide_fold", repr(e), operands, R, W, out, pl, cores, RW.MUTANT
    )
    got = (kt.Kernel.build(key, body, ports), given, nblk)
    if V.KERNELS_KEPT > 0:
        V._kernels[kkey] = got
        while len(V._kernels) > V.KERNELS_KEPT:
            V._kernels.popitem(last=False)
    return got
