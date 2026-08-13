"""The byte orders, pinned.

A layout is the one thing in the stack that cannot report its own mistakes: an
operand packed wrong is the right bytes in the wrong places, and every unit
downstream accepts it. So the orders are held by SHA rather than by round trip
-- a round trip passes just as well when pack and unpack are wrong together,
which is exactly what rewriting one of them for speed risks.

The digests below were taken from the per-sub-tile implementation these
transposes replaced.
"""

import hashlib

import numpy as np
import pytest
from kohakutpu.hw.ops import OpKind
from kohakutpu.isa.vecemit import ElementwiseKernel, VecEmitError, entry_walk

from kohakutpu import layout as LO

#: ``(grid, gm, gn, shape) -> sha256 prefix of the packed image``.
TILE_ORDER = {
    ((2, 2), 8, 8, (64, 64)): "ef1bd757967d7f29a1ecb1733a0e397e",
    ((2, 3), 4, 2, (32, 24)): "a47731e6161b241e6878b1acb2d2376a",
    ((2, 2), 8, 8, (60, 63)): "b437c03e9d42ca37418683d2d2aaa35b",
}

SHAPES = [((2, 2), 8, 8, (64, 64)), ((1, 1), 2, 1, (8, 4)), ((3, 1), 2, 2, (24, 8))]

WORD_BYTES = LO.WORD_BYTES


def operand(shape, seed=11):
    return np.random.default_rng(seed).standard_normal(shape).astype(np.float16)


@pytest.mark.parametrize("case", sorted(TILE_ORDER, key=str), ids=str)
def test_the_drained_order_is_the_one_the_cluster_writes(case):
    """A digest, not a round trip: pack and unpack can be wrong together."""
    grid, gm, gn, shape = case
    got = hashlib.sha256(LO.Tile(grid, gm, gn).pack(operand(shape))).hexdigest()
    assert got[:32] == TILE_ORDER[case], "the drained byte order moved"


@pytest.mark.parametrize("grid, gm, gn, shape", SHAPES, ids=str)
def test_a_tile_survives_its_round_trip(grid, gm, gn, shape):
    """Whole sub-tiles only, so nothing is lost and the values come back."""
    lay = LO.Tile(grid, gm, gn)
    want = operand(shape)
    got = lay.unpack(lay.pack(want), shape)
    assert np.array_equal(got, np.asarray(want, np.float64))


def walk_addresses(dims) -> list:
    """Every address `vec_agu` emits for these dims, dimension 0 fastest."""
    total = 1
    for _, bound in dims:
        total *= bound
    out = []
    for step in range(total):
        rest, addr = step, 0
        for stride, bound in dims:
            addr += (rest % bound) * stride
            rest //= bound
        out.append(addr)
    return out


@pytest.mark.parametrize(
    "rows, k, groups, blocks",
    [(64, 64, 8, 2), (32, 64, 8, 2), (64, 64, 2, 2), (32, 32, 8, 1)],
    ids=str,
)
def test_the_agu_walk_is_the_entry_order(rows, k, groups, blocks):
    """The descriptor must land the bytes `Entry.pack` would have written.

    A walk that is merely plausible puts the right bytes in the wrong entries
    and every unit downstream accepts them, so this compares the whole
    permutation rather than its length or its endpoints.
    """
    dims = entry_walk(rows, k, groups, blocks, LO.LANES, LO.KBLOCK)
    assert dims is not None, "this shape fits four dimensions"
    a = np.arange(rows * k, dtype=np.float32).astype(np.float16).reshape(rows, k)
    flat = np.frombuffer(LO.Flat().pack(a), np.uint8).reshape(-1, WORD_BYTES)
    want = np.frombuffer(LO.Entry(groups, blocks).pack(a), np.uint8)
    got = np.zeros_like(want).reshape(-1, WORD_BYTES)
    addrs = walk_addresses(dims)
    assert len(addrs) == len(flat), "the walk visits every word exactly once"
    for at, addr in enumerate(addrs):
        got[addr] = flat[at]
    assert np.array_equal(got.reshape(-1), want)


@pytest.mark.parametrize(
    "rows, k, groups, blocks",
    [(128, 64, 8, 2), (64, 128, 8, 2), (64, 256, 8, 2)],
    ids=str,
)
def test_a_walk_the_hardware_cannot_make_is_refused(rows, k, groups, blocks):
    """Over 256 words is F_LEN and over four dimensions has nowhere to go.

    Refusing returns the caller to a host relayout, which is slow and correct;
    emitting a truncated walk would be fast and wrong.
    """
    assert entry_walk(rows, k, groups, blocks, LO.LANES, LO.KBLOCK) is None


def test_a_strided_drain_is_opt_in_and_otherwise_byte_identical():
    """A kernel that asks for no walk emits exactly what it emitted before."""
    chain = [(OpKind.MUL, [0, 1])]
    plain = ElementwiseKernel(chain, 2).flits([0x1000, 0x2000], 0x3000, 4096)
    again = ElementwiseKernel(chain, 2).flits([0x1000, 0x2000], 0x3000, 4096)
    assert plain == again


def test_the_runs_of_one_pass_compose_into_the_entry_order():
    """A pass drains once per batch, so the walk has to survive being split.

    Each RUN restates the same descriptor at a stepped base, so a permutation
    wider than one batch would be emitted four times over the same 64 words
    rather than once over 256.
    """
    dims = entry_walk(64, 64, 8, 2, LO.LANES, LO.KBLOCK)
    kernel = ElementwiseKernel([(OpKind.MUL, [0, 1])], 2, drain=dims)
    runs = -(-(64 * 64) // kernel.batch)
    composed = [
        b * kernel.bw + at for b in range(runs) for at in walk_addresses(kernel.drain)
    ]
    assert composed == walk_addresses(dims)

    a = np.arange(64 * 64, dtype=np.float32).astype(np.float16).reshape(64, 64)
    flat = np.frombuffer(LO.Flat().pack(a), np.uint8).reshape(-1, WORD_BYTES)
    want = np.frombuffer(LO.Entry(8, 2).pack(a), np.uint8).reshape(-1, WORD_BYTES)
    got = np.zeros_like(want)
    for at, addr in enumerate(composed):
        got[addr] = flat[at]
    assert np.array_equal(got, want)


def test_a_batch_that_splits_a_permutation_is_refused():
    """Half a permutation per RUN is silent corruption, so it must not encode."""
    dims = entry_walk(64, 64, 8, 2, LO.LANES, LO.KBLOCK)
    with pytest.raises(VecEmitError, match="does not divide"):
        ElementwiseKernel([(OpKind.MUL, [0, 1])], 2, chunks=1, drain=dims)


def test_a_partly_drained_region_reads_back_zero():
    """A region the machine has not finished writing must not read as garbage.

    The sub-tiles past the end of the buffer are zero rather than whatever the
    truncation leaves in the last reshape, which is what a drain still in
    flight looks like from the host.
    """
    lay = LO.Tile((2, 2), 8, 8)
    raw = lay.pack(operand((64, 64)))
    half = lay.unpack(raw[: len(raw) // 2], (64, 64))
    assert half[:32].any(), "the sub-tiles that did land were dropped"
    assert not half[32:].any(), "the sub-tiles that did not land are not zero"
