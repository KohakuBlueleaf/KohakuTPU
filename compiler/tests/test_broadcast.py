"""`Buffer.repeated()`: one buffer read again for every group of a longer one.

The only address-dependent operand this DSL has, and the one that used to fail
in the shape this project spends its guards on -- MEASURED before the fix, a
2,048-element pass against a 512-element table wrote 512 elements, left 1,536
unwritten and REPORTED SUCCESS. At more than one grid instance it refused
instead, which is the same defect wearing a different face.

Two things had to agree for that to happen and neither can be dropped:

* `_span` clamped the pass to `_reach` of every operand, and a stride-0 read
  covers ANY length -- `_agree` already exempted it and `_span` did not;
* the walk's outer dimension stepped the base by one period per group, which
  walks off the end of a buffer that IS one period.
"""

import numpy as np
import pytest
from kohakuaccel.lang import dims, units
from kohakutpu.lang import kernel
from kohakutpu.model import SimDevice

from kohakutpu import lang as L

R, W, K = dims("R, W, K")


@kernel
def scaled(x=L.In(..., R, W), t=L.In(K), y=L.Out(..., R, W), *, part=2048):
    """`x * t`, with `t` read again for every `len(t)` elements of `x`."""
    with units(x.parts(part)) as e:
        y[e] <<= x[e] * t.repeated()[e]


def run(rows, wide, table, part=2048):
    dev = SimDevice()
    rng = np.random.default_rng(0)
    x = rng.standard_normal((rows, wide)).astype(np.float16)
    t = (1.0 + rng.standard_normal(table) * 0.1).astype(np.float16)
    got = np.asarray(scaled(dev.tensor(x), dev.tensor(t), part=part).numpy())
    span = rows * wide
    want = (
        x.astype(np.float64).reshape(-1)
        * np.tile(t.astype(np.float64), -(-span // table))[:span]
    )
    return got.reshape(-1), want


@pytest.mark.parametrize(
    "rows, wide, table",
    [(16, 128, 512), (16, 128, 1024), (32, 128, 512), (64, 128, 1024)],
    ids=str,
)
def test_a_broadcast_covers_the_whole_pass(rows, wide, table):
    """Every element written, at one grid instance and at four.

    Checked as a COUNT of unwritten elements as well as an error, because the
    failure was silent: the elements it skipped read back as the zeros the
    allocator left, which no tolerance on the ones it did write would catch.
    """
    got, want = run(rows, wide, table)
    assert int((got == 0).sum()) == 0, "the pass stopped short of its own result"
    rel = np.abs(got - want) / max(float(np.abs(want).max()), 1e-30)
    assert rel.max() < 1e-2, f"p50 {np.percentile(rel, 50):.2e} max {rel.max():.2e}"


def test_the_table_is_re_read_rather_than_walked_past():
    """The second group must see the table's FIRST element again.

    A walk that steps the base by one period per group reads past the end of a
    buffer that is exactly one period, which is where the wrong values came
    from -- so this compares against a TILE of the table, not against its head.
    """
    got, want = run(16, 128, 512)
    assert np.allclose(got[:512], want[:512], rtol=1e-2)
    assert np.allclose(got[512:1024], want[512:1024], rtol=1e-2)
    assert not np.allclose(want[:512], want[512:1024]), "the test cannot tell"


def test_a_period_that_does_not_divide_the_pass_is_refused():
    """It is outside the one-pass class, so it must fail LOUDLY.

    A RUN would start part way into the table and the slice it needs wraps,
    which no single affine walk expresses. Before the fix this wrote 896 of
    2,048 elements and returned success.
    """
    from kohakutpu.lang.errors import LangError

    with pytest.raises(LangError, match="part way through a period|does not divide"):
        run(16, 128, 896)


def test_a_per_group_spread_still_advances():
    """The fix must NOT reach `per_group`, whose buffer spans the whole region.

    Both are `Spread`; only the buffer's LENGTH tells them apart. A `per_group`
    read of a full-length buffer steps one period per group and must keep doing
    so -- `kernels.wide`'s fold is built on it.
    """
    from kohakutpu.isa.vecemit import Spread

    over = Spread(period=512, take=128, held=8192)
    assert not over.wraps
    assert over.at(512, 3) == 1536
    assert over.dims(1024)[-1][0] != 0, "a full-length spread stopped advancing"

    one = Spread(period=512, take=512, held=512)
    assert one.wraps and one.at(512, 3) == 0
    assert one.dims(1024)[-1][0] == 0, "a broadcast still steps its base"
