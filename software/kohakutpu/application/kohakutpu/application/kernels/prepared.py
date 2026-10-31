"""A consumer kernel's laid-out operands, made inside it.

A relayout (`runtime.forms.Pending`: its `kernels.relayout` plans) runs in the
kernel that reads the operand: the plans' statements into a scratch in the
consumer's own row shape. One launch, where the operand alone took one (or two,
with a repitch). The rows kernels' generator is Rust's (`prepared.rows_kernel`).
"""

from kohakutpu.application.kernels import relayout as RL
from kohakutpu.language import dsl as kt


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
    return RL.emit_steps(plans, p[prefix + "x"], p, prefix, hw, dst)
