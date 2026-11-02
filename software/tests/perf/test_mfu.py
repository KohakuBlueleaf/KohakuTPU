"""MFU against hand arithmetic: a 256 x 256 x 256 matmul on two clusters."""

import mfu
import pytest

# The floor is 256^3 MACs over 1024 MAC a cycle a cluster, shared by two clusters.
MG_FLOOR = 256**3 / 1024 / 2
L2 = (
    "target ktpu.v9\n\n"
    "fn m.l2(a: mx7a[256, 256] @dram, b: mx7b[256, 256] @dram, "
    "out: f16[256, 256] @dram, t: f16[8, 8] @stage)\n"
)
# Two operands of 65,536 B and a result of 131,072 B; staging holds no DRAM traffic.
USEFUL = 65_536 * 2 + 131_072
TIMED = dict(
    work=dict(
        read_bytes=2 * 131_072,
        write_bytes=131_072,
        mover_bytes=65_536,
        macs=256**3,
        math_beats=1000,
    ),
    floor=dict(mg=MG_FLOOR, vc=0.0),
)
CONVERT = dict(work=dict(read_bytes=4096, write_bytes=4096, mover_bytes=0), floor=TIMED)


def test_useful_bytes_counts_dram_parameters():
    assert mfu.useful_bytes(L2) == USEFUL


def test_step_mfu_is_the_hand_figure():
    step = [dict(name="m", kind="matmul", cycles=10_000)]
    by_kernel = mfu.kernels(step, dict(m=TIMED), dict(m=L2))
    got = mfu.whole(by_kernel, span=20_000, peak=36.0)
    assert got["mfu_mg"] == pytest.approx(0.4096)
    assert got["mfu_vc"] == 0.0
    assert got["mfu_mem"] == pytest.approx(USEFUL / 720_000)
    # 256^3 MACs at 2 flops each, and 1000 beats on 16 lanes.
    assert got["flops"] == 2 * 16_777_216 + 16_000
    row = by_kernel["m"].row(36.0)
    assert row["mfu_mg"] == pytest.approx(0.8192)
    assert row["bus"] == pytest.approx(458_752 / 360_000)


def test_a_form_change_is_no_useful_work():
    step = [dict(name="c", kind="convert", cycles=1000)]
    by_kernel = mfu.kernels(step, dict(c=CONVERT), dict(c=L2))
    got = mfu.whole(by_kernel, span=1000, peak=36.0)
    assert got["mfu_mg"] == got["mfu_mem"] == got["flops"] == 0.0
    assert got["bus"] == pytest.approx(8192 / 36_000)


def test_a_launch_with_no_record_raises():
    step = [dict(name="m", kind="matmul", cycles=10_000)]
    with pytest.raises(KeyError):
        mfu.kernels(step, {}, dict(m=L2))
