"""Single-package reduction emitters retaining each f16 partial-store boundary."""

import json

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import prepared as P
from kohakutpu.application.kernels import relayout as RL
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.plan import emit
from kohakutpu.application.runtime import rows as RW
from kohakutpu.application.runtime import tree as TR
from kohakutpu.language import dsl as kt
from kohakutpu.language import machine as MF
from kohakutpu.stack import kernel as K


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


def column_tree_kernel(
    e, operands, R: int, W: int, out: Operand, cores: int, *, hw, made=None
):
    """Single-package column reduction tree kernel over `[R, W]` on the
    machine `hw` describes (`relayout.hw_for`)."""
    made = dict(made or {})
    prepkey = tuple(sorted((i, tuple(p.shape() for p in ps)) for i, ps in made.items()))
    operands = tuple(operands)
    reds = X.reduces(e)
    red = reds[0]
    op = TR.OP[red.op]
    post, keep = RW.substitute(e, red, X.Load(0))
    kkey = (
        "rows_col_tree",
        prepkey,
        hw,
        V.expr_key(e),
        operands,
        R,
        W,
        out,
        cores,
        op,
        V.expr_key(post),
        tuple(keep),
        RW.MUTANT,
        kt.Kernel.build,
    )
    hit = V._kernels.get(kkey)
    if hit is not None:
        V._kernels.move_to_end(kkey)
        return hit
    full = Operand("full", pitch=W)
    ports, given = {}, {}
    for i, o in enumerate(operands):
        ports[f"in{i}"], given[f"in{i}"] = V.port(o, R, W)
    if out.kind == "row":
        out_rows = max(out.rows, V.stat_words(1) * V.STAT_ROWS)
        out_shape = (out_rows,)
    else:
        out_shape = V.port(out, 1, W)[1].shape
    ports["out"] = kt.Out(*out_shape)
    given["out"] = kt.TensorSpec.make(out_shape)

    shapes = {i: given[f"in{i}"].shape for i in made}
    for i, plans in made.items():
        del ports[f"in{i}"], given[f"in{i}"]
        pp, pg = P.input_ports(plans, hw, f"p{i}_")
        ports.update(pp)
        given.update(pg)

    def body(**p):
        for i, plans in made.items():
            p[f"in{i}"] = P.input_buffer(plans, p, f"p{i}_", shapes[i], hw)
        n = R
        ops = list(operands)
        bnd = {f"in{i}": p[f"in{i}"] for i in range(len(ops))}
        cur = None
        if TR.materialized(ops, n, hw.descriptors):
            cur = kt.buffer((n, W), "f16")
            RW.emit_rows_stages(
                {**bnd, "out": cur}, red.arg, tuple(ops), n, W, full, cores
            )
        elide_final = R > 1 and post == X.Load(0) and not keep and RW.MUTANT is None
        while n > 1:
            h = n // 2
            nxt_rows = h + n % 2
            if nxt_rows == 1 and elide_final:
                target_buf = p["out"]
                target_op = out
            elif RW.MUTANT == "alias_members" and cur is not None:
                target_buf = cur
                target_op = Operand("full", pitch=W, rows=nxt_rows)
            else:
                target_buf = kt.buffer((nxt_rows, W), "f16")
                target_op = Operand("full", pitch=W, rows=nxt_rows)
            if cur is None:
                k = len(ops)
                shift_h = 0 if RW.MUTANT == "wrong_order" else h
                expr_lvl = X.Op(op, (red.arg, TR._renumber(red.arg, k)))
                lvl_ops = tuple(ops + [TR._shift(o, shift_h, W) for o in ops])
                lvl_b = {
                    **{f"in{i}": bnd[f"in{i}"] for i in range(k)},
                    **{f"in{i + k}": bnd[f"in{i}"] for i in range(k)},
                    "out": target_buf,
                }
                if n % 2 and RW.MUTANT != "drop_member":
                    last = tuple(TR._shift(o, n - 1, W) for o in ops)
                    RW.emit_rows_stages(
                        {**bnd, "out": target_buf},
                        red.arg,
                        last,
                        1,
                        W,
                        Operand("full", pitch=W, offset=h * W, rows=nxt_rows),
                        cores,
                    )
                RW.emit_rows_stages(lvl_b, expr_lvl, lvl_ops, h, W, target_op, cores)
            else:
                if RW.MUTANT == "drop_member":
                    cur, n = target_buf, nxt_rows
                    break
                expr_lvl = X.Op(op, (X.Load(0), X.Load(1)))
                lvl_ops = (
                    Operand("full", pitch=W, rows=n),
                    Operand("full", pitch=W, offset=h * W, rows=n),
                )
                if n % 2:
                    kt.copy(target_buf[h : h + 1, :], cur[n - 1 : n, :])
                RW.emit_rows_stages(
                    {"in0": cur, "in1": cur, "out": target_buf},
                    expr_lvl,
                    lvl_ops,
                    h,
                    W,
                    target_op,
                    cores,
                )
            cur, n = target_buf, nxt_rows
        if elide_final:
            return
        if cur is None:
            head = red.arg
            head_ops = list(ops)
            head_b = {f"in{i}": bnd[f"in{i}"] for i in range(len(ops))}
        else:
            head = X.Load(0)
            head_ops = [Operand("full", pitch=W, rows=1)]
            head_b = {"in0": cur}
        base = len(head_ops)
        final = TR._place(post, head, base)
        for j, i in enumerate(keep):
            head_ops.append(operands[i])
            head_b[f"in{base + j}"] = p[f"in{i}"]
        head_b["out"] = p["out"]
        RW.emit_rows_stages(head_b, final, tuple(head_ops), 1, W, out, cores)

    key = V.name(
        "rows",
        "col_tree",
        repr(e),
        operands,
        R,
        W,
        out,
        cores,
        RW.MUTANT,
        *([prepkey] if made else []),
    )
    got = (kt.Kernel.build(key, body, ports), given)
    if V.KERNELS_KEPT > 0:
        V._kernels[kkey] = got
        while len(V._kernels) > V.KERNELS_KEPT:
            V._kernels.popitem(last=False)
    return got


def call_graph(gen: str, args: dict, machine) -> dict:
    """The graph of generator call `reductions.{name}` (as ktpu-plan encodes
    it: `e`, `operands`, `R`, `W`, `out`) traced here for `machine`
    (`runtime.machine.machine_of`)."""
    e = emit.parse_expr(args["e"])
    ops = tuple(Operand(**o) for o in args["operands"])
    out = Operand(**args["out"])
    cores = machine.count("VC")
    R, W = args["R"], args["W"]
    if gen == "reductions.wide_fold_kernel":
        k, given, _ = wide_fold_kernel(e, ops, R, W, out, cores)
    else:
        hw = RL.hw_of(MF.load(machine.board), cores)
        k, given = column_tree_kernel(e, ops, R, W, out, cores, hw=hw)
    return json.loads(k.trace(given, machine=K.dsl_machine(machine)).graph_json)
