"""What `through_views` resolves an operand to, and whether it is the truth.

A descriptor that lowers is worth nothing on its own: the question is always
whether walking it reads the same elements numpy would. Every window here is
checked against the slice it stands for rather than against a remembered tuple,
so a change to the resolver that is merely different fails as loudly as one
that is wrong.
"""

import numpy as np
import pytest

import ktpu.dsl as D
from ktpu.dsl import nn
from ktpu.ir import FP16, OpKind
from ktpu.ir.sched import ScheduleError
from ktpu.passes.lower import DEEP, through_views


def spec(*s):
    return D.TensorSpec(s, FP16)


def walk(view, root: np.ndarray) -> np.ndarray:
    """The elements `through_views`' answer names, in the order it names them."""
    _root, off, _flip, stride, run, pads, dims = view
    flat = np.pad(root, [tuple(p) for p in pads]).reshape(-1) if pads else root.reshape(-1)
    if dims is None:
        dims = [(root.size // max(1, run), stride)] if stride != 1 else [(root.size, 1)]
    idx = np.zeros(1, dtype=np.int64) + off
    for extent, step in dims:
        idx = (idx[:, None] + np.arange(extent) * step).reshape(-1)
    return flat[idx]


def resolve(fn, *specs):
    """Trace `fn`, which must end in one op, and resolve THAT op's operand.

    The view itself cannot be the graph output -- a pure view has no band.
    """
    g = D.trace(fn, *specs)
    return g, through_views(g, g.producer(g.outputs[0]).inputs[0])


# ---- the windows SDXL needs ------------------------------------------------


@pytest.mark.parametrize("stride,pad", [(1, 0), (1, 1), (2, 1), (2, 0)])
def test_every_conv_tap_names_exactly_its_slice(stride, pad):
    """A tap is `xp[:, ky:ky+ho*s:s, kx:kx+wo*s:s, :]` flattened. Nine of them,
    each an offset and one or two levels of `(extent, stride)`."""
    n, h, w, c, f = 1, 8, 8, 5, 3
    ho = (h + 2 * pad - 3) // stride + 1
    x = np.arange(n * h * w * c).reshape(n, h, w, c) * 1.0
    xp = np.pad(x, ((0, 0), (pad, pad), (pad, pad), (0, 0)))

    g = D.trace(
        lambda a, b: nn.conv2d(a, b, stride=stride, padding=pad),
        spec(n, h, w, c),
        spec(3, 3, c, f),
    )
    taps = [op.inputs[0] for op in g.ops if op.kind is OpKind.MATMUL]
    assert len(taps) == 9
    for i, v in enumerate(taps):
        ky, kx = divmod(i, 3)
        want = xp[
            :, ky : ky + ho * stride : stride, kx : kx + ho * stride : stride, :
        ].reshape(-1)
        got = walk(through_views(g, v), x)
        assert np.array_equal(got, want), f"tap {ky},{kx} at stride {stride}"


def test_a_conv_filter_tap_weight_is_a_contiguous_offset():
    """`w[ky, kx]` slices axes 0 AND 1, which the outer-axis rule alone refuses
    -- but both are outer, so the result is still one contiguous span."""
    c, f = 4, 6
    wa = np.arange(3 * 3 * c * f).reshape(3, 3, c, f) * 1.0
    for ky in range(3):
        for kx in range(3):
            _g, view = resolve(lambda a, ky=ky, kx=kx: a[ky, kx] * 2.0, spec(3, 3, c, f))
            assert view[3] == 1, "a whole-slab slice is contiguous"
            assert view[1] == (ky * 3 + kx) * c * f
            assert np.array_equal(walk(view, wa), wa[ky, kx].reshape(-1))


@pytest.mark.parametrize("half", [0, 1])
def test_a_geglu_chunk_is_a_run_every_row(half):
    rows, wide = 8, 12
    x = np.arange(rows * wide).reshape(rows, wide) * 1.0
    h = wide // 2
    _g, view = resolve(lambda a: a[:, half * h : (half + 1) * h] * 2.0, spec(rows, wide))
    assert (view[1], view[3], view[4]) == (half * h, wide, h)
    assert np.array_equal(walk(view, x), x[:, half * h : (half + 1) * h].reshape(-1))


def test_one_head_of_a_packed_projection_is_a_run_every_row():
    """`reshape(L, H, dh).permute(1, 0, 2)[h]` -- the torch head split. The
    permute moves more than the last two axes, and the composition with the
    slice is still `dh` contiguous elements every `H*dh`."""
    ell, heads, dh = 6, 4, 3
    x = np.arange(ell * heads * dh).reshape(ell, heads * dh) * 1.0
    for h in range(heads):
        _g, view = resolve(
            lambda a, h=h: a.reshape(ell, heads, dh).permute(1, 0, 2)[h] * 2.0,
            spec(ell, heads * dh),
        )
        assert (view[1], view[3], view[4]) == (h * dh, heads * dh, dh)
        want = x.reshape(ell, heads, dh)[:, h].reshape(-1)
        assert np.array_equal(walk(view, x), want)


def test_a_single_column_stays_one_element_per_row():
    """The MoE gate. Still stride E, run 1, which is what the tile-order read
    in `_emit_vector` keys on."""
    _g, view = resolve(lambda a: a[:, 3:4] * 2.0, spec(8, 5))
    assert (view[1], view[3], view[4]) == (3, 5, 1)


# ---- what it refuses, and why the refusal is right -------------------------


def test_all_the_heads_at_once_need_a_level_nobody_carries():
    """`(H, L, dh)` over an `(L, H*dh)` buffer is H runs of dh, L times."""
    _g, view = resolve(
        lambda a: a.reshape(6, 4, 3).permute(1, 0, 2) * 2.0, spec(6, 12)
    )
    assert view[3] == DEEP
    assert len(view[6]) == 3


def test_a_deep_walk_is_still_named_exactly():
    """DEEP means no ENGINE walks it; the host still can, which is how a
    stride-2 conv tap gets gathered."""
    x = np.arange(6 * 12).reshape(6, 12) * 1.0
    _g, view = resolve(
        lambda a: a.reshape(6, 4, 3).permute(1, 0, 2) * 2.0, spec(6, 12)
    )
    assert np.array_equal(
        walk(view, x), x.reshape(6, 4, 3).transpose(1, 0, 2).reshape(-1)
    )


def test_a_pad_of_a_view_is_refused():
    """A border around something already sliced would have to be injected
    mid-walk, and the pad frame has to exist before anything indexes it."""
    with pytest.raises(ScheduleError, match="already a view"):
        resolve(lambda a: a[1:5].pad(((1, 1), (0, 0))) * 2.0, spec(8, 4))


def test_a_concat_is_a_view_of_neither_side():
    with pytest.raises(ScheduleError, match="concat"):
        resolve(lambda a, b: a.concat(b, axis=0) * 2.0, spec(4, 4), spec(4, 4))


def test_a_zero_width_pad_folds_away():
    _g, view = resolve(lambda a: a.pad(((0, 0), (0, 0))) * 2.0, spec(4, 4))
    assert view[5] is None and view[3] == 1


def test_a_transpose_of_the_last_two_axes_is_still_a_flip():
    """`q @ k[j].T`: the region holds the orientation the GEMM wants, and the
    packer must not transpose again."""
    _g, view = resolve(lambda a: a[8:16].transpose() * 2.0, spec(32, 8))
    assert view[2] is True
    assert view[1] == 64 and view[3] == 1
