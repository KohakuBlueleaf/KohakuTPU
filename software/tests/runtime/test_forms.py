"""What the runtime keeps between runs: forms of a buffer, memory, a replayed step."""

import numpy as np
from tinygrad import Tensor
from tinygrad.engine.jit import TinyJit

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


def test_a_replayed_step_reads_its_new_input(dev):
    step = TinyJit(lambda t: ((t * 2.0).relu() + 1.0).realize())
    for _ in range(4):
        a = R.normal(size=(16, 32))
        close(step(tensor(a)).numpy(), np.maximum(a * 2.0, 0.0) + 1.0)
