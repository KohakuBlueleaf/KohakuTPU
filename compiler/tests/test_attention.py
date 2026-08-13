"""Attention end to end on the unit models, and the addressing it depends on.

Everything here is arithmetic: `test_kernel_library` already checks that these
shapes lower, and lowering is exactly what a wrong L1 bank or a missing mask
survives. Each test below returned a legal-looking wrong answer before its fix.
"""

import numpy as np
import pytest
from kohakuaccel.lang import dims, loop, units
from kohakutpu.kernels import LOG2E, flash_attention, rmsnorm
from kohakutpu.lang import LangError, kernel
from kohakutpu.model import SimDevice

from kohakutpu import lang as L

M, K, N, KK = dims("M, K, N, KK")


def error_of(got, want) -> float:
    """Largest error relative to the reference's own scale."""
    got, want = np.asarray(got, np.float64), np.asarray(want, np.float64)
    return float(np.abs(got - want).max() / np.abs(want).max())


def attention(q, k, v, scale):
    """Softmax attention in float64, the answer everything here is measured against."""
    s = np.asarray(q, np.float64) @ np.asarray(k, np.float64).T * scale
    e = np.exp(s - s.max(-1, keepdims=True))
    return (e / e.sum(-1, keepdims=True)) @ np.asarray(v, np.float64)


def run_attention(q, k, v, **knobs):
    """`flash_attention` on a fresh SimDevice, with `q` pre-scaled and `v` packed."""
    dev, scale = SimDevice(), q.shape[-1] ** -0.5
    return flash_attention(
        dev.tensor((q * scale * LOG2E).astype(np.float16)),
        dev.tensor(k),
        dev.tensor(np.ascontiguousarray(np.swapaxes(v, -1, -2))),
        **knobs,
    ).numpy()


def operands(lq, lkv, d, seed=17):
    """``(q, k, v)`` as fp16, `q` of `(lq, d)` and the other two of `(lkv, d)`."""
    rng = np.random.default_rng(seed)
    shapes = ((lq, d), (lkv, d), (lkv, d))
    return tuple(np.asarray(rng.normal(0, 1, s), np.float16) for s in shapes)


@kernel
def pick(w=L.In(M, K), v=L.In(N, KK), o=L.Out(M, N), *, gm=2, gn=1, nk=2, chunk=0):
    """``w @ v[:, chunk]`` at rung T: one sweep reading a NAMED chunk of `v`."""
    with units(w.tiles(gm), v.tiles(gn)) as (a, b):
        acc = L.tile(gm, gn, nk)
        for c in loop(1):
            acc += w[a, c] @ v[b, chunk + c]
        o[a, b] <<= acc


@kernel
def pick_placed(
    w=L.In(M, K), v=L.In(N, KK), o=L.Out(M, N), *, gm=2, gn=1, nk=2, chunk=0
):
    """The same, at rung P: the windows and their fills written out."""
    with units(w.tiles(gm), v.tiles(gn)) as (a, b):
        ra, rb = L.region(gm, nk), L.region(gn, nk)
        acc = L.tile(gm, gn, nk)
        for c in loop(1):
            ra <<= w[a, c]
            rb <<= v[b, chunk + c]
            acc += ra @ rb
        o[a, b] <<= acc


@pytest.mark.parametrize("fn", [pick, pick_placed], ids=["tiling", "placement"])
@pytest.mark.parametrize("chunk", [0, 1])
def test_a_fill_lands_in_the_bank_its_sweep_reads(fn, chunk):
    """A chunk index that is NOT the loop counter, which is where they part.

    The bank alternates per sweep step; deriving it from the operand's chunk
    instead put this fill in the bank the GEMM does not read, and the sweep
    contracted over an untouched half of L1 -- zeros, not an error. Both rungs,
    since each names the fill differently and only one of them names it at all.
    """
    rng = np.random.default_rng(3)
    w = np.asarray(rng.standard_normal((64, 64)) * 0.3, np.float16)
    v = np.asarray(rng.standard_normal((64, 128)) * 0.3, np.float16)
    dev = SimDevice()
    got = fn(dev.tensor(w), dev.tensor(v), chunk=chunk).numpy()
    want = np.float64(w) @ np.float64(v[:, chunk * 64 : chunk * 64 + 64]).T
    assert error_of(got, want) < 5e-2
    assert np.abs(got).max() > 0.0, "the sweep read a bank nothing filled"


@pytest.mark.parametrize("lkv", [64, 128, 192])
def test_the_running_softmax_folds_over_every_key_block(lkv):
    """One key block exercises no fold at all, which is why 64 is here too."""
    q, k, v = operands(64, lkv, 64)
    got = run_attention(q, k, v, block=64)
    assert error_of(got, attention(q, k, v, 64**-0.5)) < 5e-2


def test_a_key_length_that_is_not_whole_blocks_masks_its_tail():
    """SDXL's 77 context tokens: padded to 128, and the padding contributes 0.

    Without the mask the 51 zero keys each add `2^-top` to the denominator --
    a fifth of it here, and every row of the answer is wrong by that much.
    """
    q, k, v = operands(64, 77, 64)
    kp, vp = (np.zeros((128, 64), np.float16) for _ in range(2))
    kp[:77], vp[:77] = k, v
    got = run_attention(q, kp, vp, block=64, keys=77)
    assert error_of(got, attention(q, k, v, 64**-0.5)) < 5e-2


def test_the_causal_mask_is_one_table_for_every_head():
    """A table is compiler-built and shared; batching it asked for `heads` copies."""
    rng = np.random.default_rng(5)
    made = [np.asarray(rng.normal(0, 1, (2, 64, 64)), np.float16) for _ in range(3)]
    q, k, v = made
    dev, scale = SimDevice(), 64**-0.5
    got = flash_attention(
        dev.tensor((q * scale * LOG2E).astype(np.float16)),
        dev.tensor(k),
        dev.tensor(np.ascontiguousarray(np.swapaxes(v, -1, -2))),
        block=64,
        causal=True,
    ).numpy()
    s = np.float64(q) @ np.swapaxes(np.float64(k), -1, -2) * scale
    s = np.where(np.tril(np.ones(s.shape[-2:], bool)), s, -np.inf)
    e = np.exp(s - s.max(-1, keepdims=True))
    assert error_of(got, (e / e.sum(-1, keepdims=True)) @ np.float64(v)) < 5e-2


@pytest.mark.parametrize("rows", [64, 128, 256])
def test_a_reduction_taller_than_l1_runs_as_several_passes(rows):
    """L1 holds 512 words, so 64 rows of 64 is the last one that fits at once."""
    rng = np.random.default_rng(7)
    x = np.asarray(rng.standard_normal((rows, 64)), np.float16)
    w = np.asarray(rng.standard_normal((rows, 64)) * 0.1 + 1, np.float16)
    dev = SimDevice()
    got = rmsnorm(dev.tensor(x), dev.tensor(w)).numpy()
    xf = np.float64(x)
    want = xf / np.sqrt((xf**2).mean(axis=1, keepdims=True) + 1e-5) * np.float64(w)
    assert error_of(got, want) < 2e-3


def test_a_pass_over_two_lengths_is_refused():
    """The negative control for the tail: a shorter operand is read past its end.

    `flash_attention` at a head dim other than `block` is exactly this, and it
    ran and returned numbers of order 1e4 before the compiler refused it.
    """
    q, k, v = operands(64, 64, 128)
    with pytest.raises(LangError, match="different lengths"):
        run_attention(q, k, v, block=64)


def test_flash_attention_at_the_head_dim_its_block_names():
    """The same head dim the refusal above names, with `block` set to match it."""
    q, k, v = operands(128, 128, 128)
    got = run_attention(q, k, v, block=128)
    assert error_of(got, attention(q, k, v, 128**-0.5)) < 5e-2


def test_a_block_wider_than_the_sequence_is_refused():
    """It fills tiles the operand does not have, and reads whatever is next."""
    q, k, v = operands(64, 64, 128)
    with pytest.raises(LangError, match="reaches"):
        run_attention(q, k, v, block=128)


@pytest.mark.parametrize("heads", [1, 4, 7, 10, 20])
def test_every_head_gets_its_own_answer(heads):
    """Head counts that do and do not divide the four vector cores.

    SDXL runs 10 and 20 heads; 20 divides four cores and 10 does not, and the
    remainder is where a batch axis dealt round-robin would drop or repeat one.
    Checked per head, and for duplicates -- a mean error hides both.

    ONE head is a leading axis all the same. It used to reach `Entry.padded` as
    a three-axis shape and die on `too many values to unpack`, so a single-head
    or batch-1 attention had no path at all.
    """
    rng = np.random.default_rng(3)
    q, k, v = (np.asarray(rng.normal(0, 1, (heads, 64, 64)), np.float16) for _ in "qkv")
    dev, scale = SimDevice(size=64 << 20), 64**-0.5
    got = flash_attention(
        dev.tensor((q * scale * LOG2E).astype(np.float16)),
        dev.tensor(k),
        dev.tensor(np.ascontiguousarray(np.swapaxes(v, -1, -2))),
        block=64,
    ).numpy()
    assert got.shape == (heads, 64, 64)
    for h in range(heads):
        assert error_of(got[h], attention(q[h], k[h], v[h], scale)) < 8e-2, f"head {h}"
    pairs = [
        (a, b)
        for a in range(heads)
        for b in range(a + 1, heads)
        if np.array_equal(got[a], got[b])
    ]
    assert not pairs, f"heads {pairs} got the same answer"
