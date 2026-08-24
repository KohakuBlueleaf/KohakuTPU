"""The head split as an N-partition of the projection, which deletes a permute.

MEASURED, in `demos/kohakutpu/sdxl`, at 8 host permutes per transformer block,
none of which the device can do -- a permute is not a broadcast. None of them
needs to exist:

* output column `n` of `x @ w.T` belongs to head `n // dh`, and the checkpoint
  stores `w` as `[out][in]` with the head axis already outermost, so the split
  is a RESHAPE of the weight and the batch axis rides it;
* `to_out` contracts over `heads*dh` and that axis is head-major too, so the
  join is `heads` K-chunks of ONE accumulator;
* the kernel's transposed `v` is `Wv_h @ source.T`, the operands swapped, which
  the shipped `ops.matmul` already computes.
"""

import numpy as np
import pytest
from kohakutpu.cost import time as cost_time
from kohakutpu.kernels import attn_out, flash_attention, heads_of, project_heads
from kohakutpu.model import SimDevice

from kohakutpu import ops as O

FP16 = np.float16
LOG2E = 1.4426950408889634


def rel(got, want) -> np.ndarray:
    want = np.asarray(want, np.float64)
    return np.abs(np.asarray(got, np.float64) - want) / np.abs(want).max()


def over(got, want, pct=0.01) -> float:
    return float((rel(got, want) > pct).mean())


def randn(shape, seed, scale=0.1):
    return np.asarray(np.random.default_rng(seed).standard_normal(shape) * scale, FP16)


# ----------------------------------------------------- the split is a reshape
def test_heads_of_moves_no_bytes_and_refuses_a_split_that_is_not_one():
    w = randn((640, 2048), 1)
    got = heads_of(w, 10)
    assert got.shape == (10, 64, 2048)
    assert np.array_equal(got.reshape(640, 2048), w)
    with pytest.raises(ValueError, match="head axis"):
        heads_of(w, 7)


@pytest.mark.parametrize("heads,dh", [(4, 64), (10, 64), (20, 64)])
def test_the_head_projection_is_the_WIDE_one_and_a_permute(heads, dh):
    """Bit-identical, because it is the same tiling with the N tiles relabelled.

    The wide projection tiles N in 32-column groups already, and a 64-wide head
    is two of them -- so `gn` does not have to drop and nothing is recomputed.
    """
    tokens, ctx = 128, 256
    x, w = randn((tokens, ctx), 2), randn((heads * dh, ctx), 3)

    dev = SimDevice(size=1024 << 20)
    split = project_heads(dev.tensor(x), dev.tensor(heads_of(w, heads))).numpy()

    dev2 = SimDevice(size=1024 << 20)
    wide = O.matmul(dev2.tensor(x), dev2.tensor(w)).numpy()
    permuted = np.transpose(wide.reshape(tokens, heads, dh), (1, 0, 2))

    assert split.shape == (heads, tokens, dh)
    assert np.array_equal(split, np.asarray(permuted, FP16))
    assert dev.counters.get("relayouts", 0) == 0


def test_the_head_projection_COSTS_what_the_wide_one_costs():
    """The claim `sdxl-requirements.md` 5.2 makes is that it costs MORE.

    It prices a 64-wide head at `gn = 4` against the default 8 and intensity
    5.33 against 8.00. A lane group is FOUR elements, so `gn = 8` is 32 columns
    and a 64-wide head is two whole tiles -- the tiling never changes, and the
    cycles and the flits are equal. `gn = 4` really does cost 1.32x; nothing
    asks for it.
    """
    tokens, heads, dh, ctx = 256, 20, 64, 1280
    x, w = randn((tokens, ctx), 4), randn((heads * dh, ctx), 5)
    dev = SimDevice(size=2048 << 20)
    wide = cost_time(
        O.matmul.plan(dev.tensor(x), dev.tensor(w), gm=8, gn=8), dev.machine
    )
    per_head = cost_time(
        project_heads.plan(dev.tensor(x), dev.tensor(heads_of(w, heads)), gm=8, gn=8),
        dev.machine,
    )
    narrow = cost_time(
        project_heads.plan(dev.tensor(x), dev.tensor(heads_of(w, heads)), gm=8, gn=4),
        dev.machine,
    )
    assert per_head.cycles == wide.cycles
    assert 1.3 < narrow.cycles / wide.cycles < 1.4


def test_the_transposed_v_is_the_SHIPPED_matmul_with_the_operands_swapped():
    """`flash_attention` declares `v = In(..., Dv, Lkv)`; `to_v` makes `[L][D]`.

    `Wv_h @ source.T` is that transpose COMPUTED rather than moved, and it is
    `ops.matmul` with the weight as the batch. No kernel, no permute, no pass.
    """
    tokens, heads, dh, ctx = 128, 4, 64, 256
    src, wv = randn((tokens, ctx), 6), randn((heads * dh, ctx), 7)
    dev = SimDevice(size=1024 << 20)
    got = O.matmul(dev.tensor(heads_of(wv, heads)), dev.tensor(src)).numpy()

    dev2 = SimDevice(size=1024 << 20)
    flat = O.matmul(dev2.tensor(src), dev2.tensor(wv)).numpy()
    moved = np.transpose(flat.reshape(tokens, heads, dh), (1, 2, 0))

    assert got.shape == (heads, dh, tokens)
    assert np.array_equal(got, np.asarray(moved, FP16))


# --------------------------------------------------------------- the join
@pytest.mark.parametrize("heads,dh", [(20, 64), (10, 64), (4, 128), (1, 512), (3, 192)])
def test_attn_out_is_the_join_and_the_matmul_in_one_sweep(heads, dh):
    """One K-chunk per head at `dh = 64`; eight at the VAE's single 512 head.

    The sweep step is `(head, chunk)` and only the second of those is index
    arithmetic, so the first rebinds -- and a wrong divmod reads another head's
    tile and still returns a plausible answer, which is why every chunk count
    the model uses is here.
    """
    tokens, dim = 128, 256
    o = randn((heads, tokens, dh), 8)
    w, r = randn((dim, heads * dh), 9), randn((tokens, dim), 10)
    joined = np.ascontiguousarray(o.transpose(1, 0, 2).reshape(tokens, heads * dh))

    dev = SimDevice(size=2048 << 20)
    got = attn_out(
        dev.tensor(o.reshape(heads * tokens, dh)),
        dev.tensor(w),
        dev.tensor(r),
        heads=heads,
    ).numpy()

    dev2 = SimDevice(size=2048 << 20)
    want = np.float64(joined) @ np.float64(w).T + np.float64(r)
    ref = O.matmul(dev2.tensor(joined), dev2.tensor(w)).numpy() + np.float64(r)

    assert over(got, want) <= over(ref, want) + 0.001
    assert dev.saturated == 0


# ------------------------------------------------- the block, end to end
def attention(dev, x, ctx, ws, heads, dh, block):
    """One CrossAttention with nothing permuted at run time."""
    wq, wk, wv, wo = ws
    scale = np.float16(LOG2E / np.sqrt(dh))
    q = project_heads(dev.tensor(x * scale), dev.tensor(heads_of(wq, heads)))
    k = project_heads(dev.tensor(ctx), dev.tensor(heads_of(wk, heads)))
    v = O.matmul(dev.tensor(heads_of(wv, heads)), dev.tensor(ctx))
    o = flash_attention(q, k, v, block=block)
    zero = dev.tensor(np.zeros((x.shape[0], wo.shape[0]), FP16))
    return attn_out(
        o.reshape(heads * x.shape[0], dh), dev.tensor(wo), zero, heads=heads
    ).numpy()


def reference(x, ctx, ws, heads, dh):
    wq, wk, wv, wo = ws
    xf, cf = np.float64(x), np.float64(ctx)
    q = (xf @ np.float64(wq).T).reshape(-1, heads, dh).transpose(1, 0, 2)
    k = (cf @ np.float64(wk).T).reshape(-1, heads, dh).transpose(1, 0, 2)
    v = (cf @ np.float64(wv).T).reshape(-1, heads, dh).transpose(1, 0, 2)
    s = q @ k.transpose(0, 2, 1) / np.sqrt(dh)
    e = np.exp(s - s.max(-1, keepdims=True))
    o = (e / e.sum(-1, keepdims=True)) @ v
    return o.transpose(1, 0, 2).reshape(-1, heads * dh) @ np.float64(wo).T


@pytest.mark.parametrize("ctxdim", [256, 512])
def test_a_whole_attention_runs_with_ZERO_host_permutes(ctxdim):
    """Self at `ctxdim == dim` and cross at anything else; 8 permutes per block.

    What is left over is `tile -> entry` on `q`, `k` and `v`: the projections
    DRAIN in sub-tile order and the kernel FILLS in entry order. That is an
    ordinary conversion and it RUNS ON CARD -- a host permute never could, and
    asking for one now raises `RelayoutError` rather than quietly paying it.
    """
    tokens, dim, heads, dh = 128, 256, 4, 64
    x, ctx = randn((tokens, dim), 11), randn((tokens, ctxdim), 12)
    ws = (
        randn((heads * dh, dim), 13),
        randn((heads * dh, ctxdim), 14),
        randn((heads * dh, ctxdim), 15),
        randn((dim, heads * dh), 16),
    )
    dev = SimDevice(size=4096 << 20)
    got = attention(dev, x, ctx, ws, heads, dh, 64)
    want = reference(x, ctx, ws, heads, dh)
    # The known baseline: ~20-25% of elements past 1% at this depth, and none
    # past 10%. Asserted as a SHARE, since a max reports the fp16 defect.
    assert over(got, want) < 0.30
    assert over(got, want, 0.10) == 0.0
    assert dev.saturated == 0
    assert dev.counters.get("relayouts", 0) == 0, "a host round trip is banned"
