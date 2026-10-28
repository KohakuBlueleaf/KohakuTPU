"""A rows kernel whose laid-out operands it makes itself.

`vector.rows_kernel` reads every operand from a port. Here an operand a
relayout makes (`runtime.forms.Pending`: its `kernels.relayout` plans) is
made inside the kernel first: the plans' statements into a scratch of
lines, copied whole into a scratch shaped as the operand's port, then the
rows stages read that scratch. One launch, where the operand alone took one
(or two, with a repitch).
"""

from collections import OrderedDict

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import relayout as RL
from kohakutpu.application.kernels import vector as V
from kohakutpu.language import dsl as kt

KERNELS_KEPT = 1024
_kernels: OrderedDict = OrderedDict()


def rows_kernel(
    e, operands: tuple, R: int, W: int, out, cores: int, made: dict, hw, alias=None
):
    """`vector.rows_kernel` of `e` with operand i (a key of `made`) made by
    the plans ``made[i]`` (the first from port ``x{i}``, their tables
    ``p{i}_index{j}`` / ``p{i}_mask{j}``), and operand i of `alias` reading
    operand ``alias[i]``'s made buffer (at its own offset); a reduction
    across column blocks leaves its partials, as there. Returns ``(kernel,
    given)``."""
    alias = dict(alias or {})
    operands = tuple(operands)
    kkey = (
        "prepared_rows",
        V.expr_key(e),
        operands,
        R,
        W,
        out,
        cores,
        tuple(sorted((i, tuple(p.shape() for p in ps)) for i, ps in made.items())),
        tuple(sorted(alias.items())),
        hw,
        kt.Kernel.build,
    )
    hit = _kernels.get(kkey)
    if hit is not None:
        _kernels.move_to_end(kkey)
        return hit
    reduces = X.reduces(e)
    quantum = V.STAT_ROWS if V.holds_stat(operands, out.kind) else 4
    pl = V.plan(operands, out.kind, R, W, quantum=quantum)
    if pl.nblk > 1 and len(reduces) > 1:
        raise ValueError("several reductions over a row wider than one block")
    parts = V.stages(pl, R, quantum)
    ports, given, shapes = {}, {}, {}
    for i, o in enumerate(operands):
        port, spec = V.port(o, R, W)
        if i in made:
            plans = made[i]
            ports[f"x{i}"] = kt.In(plans[0].src_words, hw.lanes)
            given[f"x{i}"] = kt.TensorSpec.make((plans[0].src_words, hw.lanes))
            tp, tg = RL.step_ports(plans, f"p{i}_")
            ports |= tp
            given |= tg
        if i in made or i in alias:
            # The made buffer covers every row any of its readers reads.
            j = alias.get(i, i)
            held = shapes.get(j)
            shapes[j] = spec.shape if held is None else max(held, spec.shape)
        else:
            ports[f"in{i}"], given[f"in{i}"] = port, spec
    if out.kind == "row":
        rows = max(out.rows, V.stat_words(R) * V.STAT_ROWS)
        out_shape = (pl.nblk, rows) if pl.nblk > 1 else (rows,)
    else:
        out_shape = V.port(out, R, W)[1].shape
    ports["out"] = kt.Out(*out_shape)
    given["out"] = kt.TensorSpec.make(out_shape)

    def body(**p):
        bufs = dict(p)
        for i, plans in made.items():
            lines = RL.emit_steps(plans, p[f"x{i}"], p, f"p{i}_", hw)
            shape = shapes[i]
            values = 1
            for s in shape:
                values *= s
            words = min(-(-values // hw.lanes), plans[-1].dst_words)
            rows = RL.scratch_rows(shape[0], values // shape[0], hw)
            laid = kt.buffer((rows, *shape[1:]), "f16")
            kt.copy(laid, lines, walk=((words, hw.word),), dwalk=((words, hw.word),))
            bufs[f"in{i}"] = laid
        for i, j in alias.items():
            bufs[f"in{i}"] = bufs[f"in{j}"]
        for sp, row0, rows in parts:
            if rows is None:
                V.rows_stage(bufs, e, operands, out, sp, cores, row0=row0)
            else:
                V._tail_band(bufs, e, operands, out, sp, row0, rows, W)

    name = V.name("rows", repr(e), operands, R, W, out, pl, kkey[7])
    got = (kt.Kernel.build(name, body, ports), given)
    if KERNELS_KEPT > 0:
        _kernels[kkey] = got
        while len(_kernels) > KERNELS_KEPT:
            _kernels.popitem(last=False)
    return got


def direct_destination(plans):
    """The final plan addresses its destination in bytes/words, not row indices."""
    return all(
        isinstance(p, (RL.Fill, RL.Words, RL.Runs, RL.Pull))
        for p in plans[-1].parts
        if p.dst == "dst"
    )


def input_ports(plans, hw, prefix):
    ports = {prefix + "x": kt.In(plans[0].src_words, hw.lanes)}
    given = {prefix + "x": kt.TensorSpec.make((plans[0].src_words, hw.lanes))}
    tp, tg = RL.step_ports(plans, prefix)
    return ports | tp, given | tg


def input_buffer(plans, p, prefix, shape, hw):
    """Produce the consumer's own row shape directly, with no shape-copy stage."""
    width = shape[-1]
    rows = max(
        shape[0] if len(shape) == 2 else 1, -(-plans[-1].dst_words * hw.lanes // width)
    )
    rows = RL.scratch_rows(rows, width, hw)
    dst = kt.buffer((rows, width) if len(shape) == 2 else (rows * width,), "f16")
    return RL.emit_steps(plans, p[prefix + "x"], p, prefix, hw, destination=dst)


__all__ = ["rows_kernel"]
