"""What the runtime keeps between runs: forms of a buffer, memory, a replayed step."""

from typing import cast

import numpy as np
from tinygrad.device import Buffer
from tinygrad.engine.jit import TinyJit
from tinygrad.helpers import Context
from tinygrad.tensor import Tensor

from kohakutpu.application.ktpugrad.captured import recipe
from kohakutpu.application.ktpugrad.run.regions import _identity
from kohakutpu.application.runtime.forms import F16
from kohakutpu.application.runtime.memory.storage import Storage, StorageView

R = np.random.default_rng(0)
#: Of the largest value: contractions read seven bits a block.
BOUND = 0.05


def close(got, want):
    assert np.abs(got.astype(np.float64) - want).max() <= BOUND * np.abs(want).max()


def tensor(x):
    return Tensor(x.astype(np.float16), device="KTPU").realize()


def test_memory_reads_back_what_was_written(dev):
    raw = R.bytes(4096)
    at = dev.alloc(len(raw))
    dev.write(at, raw)
    assert dev.read(at, len(raw)) == raw
    dev.free(at)


def test_a_cached_form_follows_a_new_upload(dev):
    a, b = R.normal(size=(16, 32)), R.normal(size=(32, 32))
    x, w = tensor(a), tensor(b)
    close((x @ w).numpy(), a @ b)
    fresh = R.normal(size=(32, 32))
    w.assign(tensor(fresh)).realize()
    close((x @ w).numpy(), a @ fresh)


def test_a_form_reads_back_flat(dev):
    a, b = R.normal(size=(16, 32)), R.normal(size=(32, 32))
    x, w = tensor(a), tensor(b)
    (x @ w).realize()
    close(x.numpy().astype(np.float64), a)
    close(w.numpy().astype(np.float64), b)


def kinds(dev, run):
    """The kinds of the launches `run` submits."""
    seen = []
    submit = dev.run

    def counted(compiled, bindings, kind, info):
        seen.append(kind)
        return submit(compiled, bindings, kind, info)

    dev.run = counted
    try:
        run()
    finally:
        dev.run = submit
    return seen


def test_a_captured_softmax_is_one_kernel_and_keeps_what_it_returns(dev, monkeypatch):
    plans = []
    submit = recipe.submit

    def spied(rt, plan, buffers):
        plans.append(plan.name)
        return submit(rt, plan, buffers)

    monkeypatch.setattr(recipe, "submit", spied)
    step = TinyJit(lambda x: (x.softmax(-1), x.sum(-1)))
    a = R.normal(size=(8, 32))
    tp = np.exp(a) / np.exp(a).sum(-1, keepdims=True)
    got = None, None
    for _ in range(4):
        out, total = step(tensor(a).contiguous().realize())
        got = out.numpy(), total.numpy()
    close(got[0], tp)
    close(got[1], a.sum(-1))
    assert any(name.endswith("_region") for name in plans)


def test_a_contraction_of_unfolded_outer_axes_runs_as_a_product(dev):
    a, b = R.normal(size=(6, 5, 5, 16)), R.normal(size=(8, 16, 10, 10))
    x, w = tensor(a), tensor(b)
    form = "ijkl,mlno->ijkmno"
    want = np.einsum(form, a, b)
    seen = kinds(dev, lambda: close(Tensor.einsum(form, x, w).numpy(), want))
    assert "matmul" in seen, seen


def windows(a, k):
    """Every k x k window of `a`'s last two axes, as `[.., o, o, k, k]`."""
    return np.lib.stride_tricks.sliding_window_view(a, (k, k), axis=(-2, -1))


def test_a_pool_reads_through_its_repeats(dev):
    a = R.normal(size=(4, 3, 14, 14))
    x = tensor(a)
    want = windows(a, 5).mean((-2, -1))
    seen = kinds(dev, lambda: close(x.avg_pool2d((5, 5), 1).numpy(), want))
    assert seen.count("convert") + seen.count("move") <= 1, seen


def test_a_convolution_reads_through_its_repeats(dev):
    a, b = R.normal(size=(4, 3, 14, 14)), R.normal(size=(2, 3, 5, 5))
    want = np.einsum("bcijkl,ockl->boij", windows(a, 5), b)
    close(tensor(a).conv2d(tensor(b)).numpy(), want)


def test_a_pool_of_an_activation_computes_only_what_it_reads(dev, monkeypatch):
    sizes = []
    submit = recipe.submit

    def spied(rt, plan, buffers):
        sizes.append(plan.out_n)
        return submit(rt, plan, buffers)

    monkeypatch.setattr(recipe, "submit", spied)
    a = R.normal(size=(4, 3, 14, 14))
    want = windows(np.maximum(a, 0) * 2 + 1, 5).max((-2, -1))
    got = cast(Tensor, (tensor(a).relu() * 2 + 1).max_pool2d((5, 5), 1))
    seen = kinds(dev, lambda: close(got.numpy(), want))
    assert seen.count("convert") + seen.count("move") <= 1, seen
    assert sizes and max(sizes) < 25 * a.size, sizes


def test_a_read_through_keeps_the_bytes_its_source_held(dev):
    a = R.normal(size=(8, 32))
    x = tensor(a)
    repeats = x.repeat((3, 1)).contiguous()
    y = (repeats[4:20] * 2.0).contiguous()
    x.assign(x + 1.0)
    Tensor.realize(y, x)
    close(y.numpy(), 2 * np.tile(a, (3, 1))[4:20])
    close(x.numpy(), a + 1)


def test_a_buffer_a_tensor_names_is_made_though_reads_go_through_it(dev):
    a = R.normal(size=(8, 32))
    x = tensor(a)
    repeats = x.repeat((3, 1)).contiguous()
    y = (repeats[4:20] * 2.0).contiguous()
    Tensor.realize(y, repeats)
    close(y.numpy(), 2 * np.tile(a, (3, 1))[4:20])
    close(repeats.numpy(), np.tile(a, (3, 1)))


def test_a_transpose_between_whole_tiles_keeps_every_value(dev):
    a = R.normal(size=(784, 128)).astype(np.float16)
    assert (tensor(a).permute(1, 0).contiguous().numpy() == a.T).all()


def test_a_read_through_keeps_what_a_later_batch_reads(dev):
    def fn(x):
        y = x.permute(1, 0).contiguous()
        z = (y * 2.0).contiguous()
        for _ in range(4):
            z = (z + 1.0).contiguous()
        return (z - y).realize()

    a = R.normal(size=(16, 32))
    with Context(JIT_BATCH_SIZE=2):
        step = TinyJit(fn)
        got = step(tensor(a).contiguous().realize())
        for _ in range(3):
            got = step(tensor(a).contiguous().realize())
    close(got.numpy(), a.T + 4)


def test_one_row_from_a_read_past_a_word_start(dev):
    a, b = R.normal(size=(1, 256)), R.normal(size=(1, 256))
    y = tensor(a)[:, 3:131].contiguous() * 2.0 + 1.0
    z = (tensor(b)[:, 3:131] + y).reshape(1, 128).contiguous()
    close(z.numpy(), b[:, 3:131] + a[:, 3:131] * 2 + 1)


def test_a_transpose_of_small_sides_keeps_every_value(dev):
    a = R.normal(size=(3, 10, 10, 30, 5)).astype(np.float16)
    got = tensor(a).permute(0, 3, 1, 4, 2).contiguous().numpy()
    assert (got == a.transpose(0, 3, 1, 4, 2)).all()


def test_a_row_shorter_than_a_band_past_a_stride(dev):
    a, b = R.normal(size=(1, 100352)), R.normal(size=(1, 100352))
    close((tensor(a) * tensor(b) + 1).numpy(), a * b + 1)


def test_a_weights_transpose_and_packing_are_made_once(dev):
    b = R.normal(size=(96, 64))
    w = tensor(b)
    outs = [R.normal(size=(32, 96)) for _ in range(3)]
    seen, y = [], w
    for a in outs:
        x = tensor(a)
        y = x @ w.permute(1, 0).reshape(64, 96).permute(1, 0)
        seen.append(kinds(dev, lambda y=y, a=a: close(y.numpy(), a @ b)))
    made = {"transpose", "move"}
    assert made & set(seen[0])
    assert not made & set(seen[1]) and not made & set(seen[2])
    assert seen[1].count("quant") == seen[2].count("quant") < seen[0].count("quant")
    close(y.numpy(), outs[2] @ b)


def test_a_kept_read_follows_a_new_upload(dev):
    a, b = R.normal(size=(32, 96)), R.normal(size=(96, 64))
    w = tensor(b)
    tn = tensor(a)
    close((tn @ w.permute(1, 0).reshape(64, 96).permute(1, 0)).numpy(), a @ b)
    fresh = R.normal(size=(96, 64))
    w.assign(tensor(fresh)).realize()
    close((tn @ w.permute(1, 0).reshape(64, 96).permute(1, 0)).numpy(), a @ fresh)


def test_a_new_upload_makes_a_kept_read_again(dev):
    b = R.normal(size=(64, 96))
    w, a = tensor(b), R.normal(size=(32, 64))
    x = tensor(a)
    close((x @ w.permute(1, 0).contiguous().permute(1, 0)).numpy(), a @ b)
    fresh = R.normal(size=(64, 96))
    w.assign(tensor(fresh)).realize()
    close((x @ w.permute(1, 0).contiguous().permute(1, 0)).numpy(), a @ fresh)


def test_a_write_to_a_kept_reads_output_leaves_the_kept_read(dev):
    b = R.normal(size=(64, 96))
    w = tensor(b)
    read = lambda: w.permute(1, 0).contiguous().realize()
    first, second = read(), read()
    second.assign(tensor(R.normal(size=(96, 64)))).realize()
    close(first.numpy(), b.T)
    close(read().numpy(), b.T)


def test_a_write_to_a_kept_reads_first_output_leaves_the_kept_read(dev):
    b = R.normal(size=(64, 96))
    w = tensor(b)
    read = lambda: w.permute(1, 0).contiguous().realize()
    first, second = read(), read()
    first.assign(tensor(R.normal(size=(96, 64)))).realize()
    close(second.numpy(), b.T)
    close(read().numpy(), b.T)


def test_a_partial_upload_keeps_the_rest_of_a_form_only_owner(dev):
    st = form_only(dev)
    allocator = cast(Buffer, tensor(np.zeros(16)).uop.buffer).allocator
    raw = np.array([99], np.float32).tobytes()
    allocator._copyin(StorageView(st, 4, 4), memoryview(raw))
    got = np.frombuffer(dev.read(st.addr, 64), np.float32)
    assert got.tolist() == [1, 99, *range(3, 17)]


def test_a_view_reads_its_own_bytes_of_a_form_only_owner(dev):
    st = form_only(dev)
    allocator = cast(Buffer, tensor(np.zeros(16)).uop.buffer).allocator
    got = bytearray(8)
    allocator._copyout(memoryview(got), StorageView(st, 4, 8))
    assert np.frombuffer(bytes(got), np.float32).tolist() == [2, 3]


def test_a_region_reads_a_whole_view_as_its_storage(dev):
    st = form_only(dev)
    assert _identity(StorageView(st, 0, 64)) == _identity(st)
    assert _identity(StorageView(st, 4, 8)) != _identity(st)


def form_only(dev):
    """An f32 owner of 1..16 whose only current form is f16."""
    st = Storage(dev, 64)
    f16 = st.form(F16, 32)
    dev.write(f16.addr, np.arange(1, 17, dtype=np.float16).tobytes())
    st.valid, st.dtype = {F16}, np.float32
    return st


def test_a_softmax_is_one_kernel(dev):
    a = R.normal(size=(16, 32))
    x = tensor(a)
    want = np.exp(a) / np.exp(a).sum(-1, keepdims=True)
    runs = kinds(dev, lambda: close(x.softmax(-1).numpy(), want))
    assert len(runs) == 1


def test_a_layernorm_is_one_kernel(dev):
    a = R.normal(size=(16, 64))
    x = tensor(a)
    c = a - a.mean(-1, keepdims=True)
    want = c / np.sqrt((c * c).mean(-1, keepdims=True) + 1e-5)

    def norm():
        m = x.mean(-1, keepdim=True)
        d = x - m
        return d / ((d * d).mean(-1, keepdim=True) + 1e-5).sqrt()

    runs = kinds(dev, lambda: close(norm().numpy(), want))
    assert len(runs) == 1


def test_a_region_keeps_an_output_a_tensor_names(dev):
    a = R.normal(size=(16, 32))
    x = tensor(a)
    d = (x - x.mean(-1, keepdim=True)).realize()
    y = (d * d).sum(-1, keepdim=True) * d
    c = a - a.mean(-1, keepdims=True)
    close(y.numpy(), (c * c).sum(-1, keepdims=True) * c)
    close(d.numpy(), c)


def test_a_replayed_step_reads_its_new_input(dev):
    step = TinyJit(lambda t: ((t * 2.0).relu() + 1.0).realize())
    for _ in range(4):
        a = R.normal(size=(16, 32))
        close(step(tensor(a)).numpy(), np.maximum(a * 2.0, 0.0) + 1.0)
