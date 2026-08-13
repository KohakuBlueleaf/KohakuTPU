"""A per-channel operand read INSIDE a fused epilogue, beside the tile.

`y[i, j] <<= acc + bias[j]` with `bias=L.In(N)`. It used to be refused, and the
refusal said a second operand "would need a fill of its own" as though that
were a hardware fact. It is not: the core uses 3 of its 8 descriptors for a
fused epilogue and `vec_agu` has the dimensions.

WHAT IT REPLACES: a full-shape bias, broadcast on the host to `M*N` so an
elementwise pass could read operands of one length. That costs M times the DRAM
to store N distinct values, and a whole pass over the result.

Fixtures are defined here (rule 2b).
"""

import numpy as np
import pytest
from kohakuaccel.lang import dims, loop, units
from kohakutpu.lang import kernel

from kohakutpu import api
from kohakutpu import lang as L
from kohakutpu import layout as LO

M, K, N = dims("M, K, N")


@kernel
def linear_bias(
    x=L.In(..., M, K),
    w=L.In(N, K),
    bias=L.In(N),
    y=L.Out(..., M, N),
    *,
    gm=8,
    gn=8,
    nk=2,
):
    """``x @ w.T + bias``, the bias riding the resident tile."""
    with units(x.tiles(gm), w.tiles(gn)) as (i, j):
        acc = L.tile(gm, gn, nk)
        for k in loop(x.chunks32(nk)):
            acc += x[i, k] @ w[j, k]
        y[i, j] <<= acc + bias[j]


@kernel
def linear_bias_scaled(
    x=L.In(..., M, K),
    w=L.In(N, K),
    bias=L.In(N),
    y=L.Out(..., M, N),
    *,
    gm=8,
    gn=8,
    nk=2,
    s=0.5,
):
    """A LINEAR chain over the biased tile, which needs no second register."""
    with units(x.tiles(gm), w.tiles(gn)) as (i, j):
        acc = L.tile(gm, gn, nk)
        for k in loop(x.chunks32(nk)):
            acc += x[i, k] @ w[j, k]
        y[i, j] <<= (acc + bias[j]) * s


def operands(m=64, k=128, n=64, seed=0):
    rng = np.random.default_rng(seed)
    return (
        np.asarray(rng.normal(0, 0.3, (m, k)), np.float16),
        np.asarray(rng.normal(0, 0.3, (n, k)), np.float16),
        np.asarray(rng.normal(0, 0.5, (n,)), np.float16),
    )


def run(kern, arrays, **knobs):
    d = api.script_device("sim")
    return np.float64(np.asarray(kern(*[d.tensor(a) for a in arrays], **knobs).numpy()))


@pytest.mark.parametrize(("m", "k", "n"), [(64, 128, 64), (32, 64, 96)])
def test_the_bias_lands_on_the_right_channels(m, k, n):
    """A per-channel read is only right if it is right PER CHANNEL.

    A bias off by one column group still looks plausible, so this grades
    against float64 rather than checking that something was added.
    """
    x, w, b = operands(m, k, n)
    got = run(linear_bias, (x, w, b))
    want = np.float64(x) @ np.float64(w).T + np.float64(b)
    assert np.abs(got - want).max() / np.abs(want).max() < 5e-2


def test_it_costs_NO_temp_and_no_second_pass():
    """THE point. Staged, this is a DRAM round trip and an elementwise pass."""
    d = api.script_device("sim")
    got = linear_bias.plan(*[d.tensor(a) for a in operands()])
    assert not got.temps, sorted(got.temps)
    assert [s.unit for s in got.stages] == ["MG", "VC"]


def test_a_linear_chain_over_the_biased_tile_rides_along():
    x, w, b = operands()
    got = run(linear_bias_scaled, (x, w, b))
    want = (np.float64(x) @ np.float64(w).T + np.float64(b)) * 0.5
    assert np.abs(got - want).max() / np.abs(want).max() < 5e-2


def test_an_epilogue_READING_THE_BIASED_TILE_TWICE_is_refused_for_now():
    """`silu(acc + bias)` reads the sum twice, and this path keeps ONE running
    result. `BandKernel` lifts a chain into a held register; the resident
    epilogue has no equivalent, so the gap is the emitter's, not the machine's.
    """

    @kernel
    def acts(
        x=L.In(M, K),
        w=L.In(N, K),
        bias=L.In(N),
        y=L.Out(M, N),
        *,
        gm=8,
        gn=8,
        nk=2,
    ):
        with units(x.tiles(gm), w.tiles(gn)) as (i, j):
            acc = L.tile(gm, gn, nk)
            for k in loop(x.chunks32(nk)):
                acc += x[i, k] @ w[j, k]
            v = acc + bias[j]
            y[i, j] <<= v * L.recip(L.exp2(v * -1.4426950408889634) + 1.0)

    d = api.script_device("sim")
    xa, wa, ba = operands()
    with pytest.raises(L.LangError, match="ONE running result"):
        acts.plan(d.tensor(xa), d.tensor(wa), d.tensor(ba))


def test_the_bias_is_stored_per_channel_not_broadcast():
    """`ChannelBias` is `4n` elements; the full-shape workaround was `M*n`."""
    d = api.script_device("sim")
    got = linear_bias.plan(*[d.tensor(a) for a in operands()])
    order = got.layouts["bias"]
    assert isinstance(order, LO.ChannelBias)
    assert order.nbytes((64,)) == 64 * LO.LANES * 2
    assert order.nbytes((64,)) < 64 * 64 * 2, "no smaller than the broadcast"


def test_the_packed_word_repeats_a_column_group_down_its_rows():
    """One 4x4 sub-tile is one word, so a column's bias sits in all four rows."""
    order = LO.ChannelBias(8)
    raw = order.pack(np.arange(32, dtype=np.float16))
    words = np.frombuffer(raw, np.float16).reshape(-1, LO.LANES, LO.LANES)
    assert words.shape[0] == 8
    assert np.array_equal(words[0], np.tile(np.arange(4, dtype=np.float16), (4, 1)))
    assert np.array_equal(words[1][0], np.arange(4, 8, dtype=np.float16))


def test_the_repeat_dimension_is_present_and_is_not_decoration():
    """`_walk` CLAMPS at its last index, so without the `gm` stride-0 dim every
    read past `gn - 1` sticks there and the tile's lower rows read one column."""
    from kohakutpu.hw import vector as V
    from kohakutpu.isa.vecemit import ResidentEpilogueKernel

    made = ResidentEpilogueKernel([], 0, {}, 16, operand=1, gm=4, gn=4)
    descs = made.static_descs()
    assert V.desc_flit(made.AD_BREAD, 2, V.dim(0, 4)) in descs


def test_two_operands_beside_the_tile_are_refused():
    """One core fills one of them."""

    @kernel
    def twice(
        x=L.In(M, K),
        w=L.In(N, K),
        a=L.In(N),
        b=L.In(N),
        y=L.Out(M, N),
        *,
        gm=8,
        gn=8,
        nk=2,
    ):
        with units(x.tiles(gm), w.tiles(gn)) as (i, j):
            acc = L.tile(gm, gn, nk)
            for k in loop(x.chunks32(nk)):
                acc += x[i, k] @ w[j, k]
            y[i, j] <<= acc + a[j] + b[j]

    d = api.script_device("sim")
    xa, wa, ba = operands()
    with pytest.raises(L.LangError, match="one core fills ONE"):
        twice.plan(d.tensor(xa), d.tensor(wa), d.tensor(ba), d.tensor(ba))
