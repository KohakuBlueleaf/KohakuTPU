"""A statistic's memory form is compact; full rows are explicit broadcasts."""

import pytest

from kohakutpu.language import dsl as kt
from kohakutpu.language import machine as MF

MACHINE = kt.Machine.of(MF.load("multimesh_v9"))


@kt.kernel
def statistic(
    x=kt.In(16, 32), y=kt.Out(16), z=kt.Out(16, 32), *, form="word", columns=32
):
    for c in kt.par(1, on=kt.vc):
        for t in kt.range(1):
            s = kt.rmax(kt.load(x, gt4=True))
            if form == "band":
                kt.pack(s, gt4=True)
            elif form == "store":
                kt.store(z, s)
            elif form == "broadcast":
                kt.store(z, kt.broadcast(s, columns))
            else:
                kt.store(y, s)


@pytest.mark.parametrize("form", ["band", "store"])
def test_band_pack_and_implicit_band_store_are_refused(form):
    with pytest.raises(kt.DSLError):
        statistic.trace({"x": ((16, 32), "f16")}, machine=MACHINE, form=form)


def test_compact_pack_and_explicit_row_broadcast():
    text = statistic.trace({"x": ((16, 32), "f16")}, machine=MACHINE).text
    assert "f16[16] @l1" in text
    text = statistic.trace(
        {"x": ((16, 32), "f16")}, machine=MACHINE, form="broadcast"
    ).text
    assert "mul s1[:, *], 1.0" in text
    assert "f32[16, 32] @reg(gt4)" in text
    assert "pack s1" not in text


@pytest.mark.parametrize("columns", [0, -32, 17])
def test_broadcast_requires_whole_column_blocks(columns):
    with pytest.raises(kt.DSLError, match="whole 32-column blocks"):
        statistic.trace(
            {"x": ((16, 32), "f16")}, machine=MACHINE, form="broadcast", columns=columns
        )
