"""A product's epilogue writes the MXFP7 packings its consumers read
(`forms.demands`): learned on the first call, from the second the consumers'
quant launches are gone, each packing byte-equal to the quant's."""

from typing import cast

import numpy as np
import pytest
from tinygrad.device import Buffer
from tinygrad.tensor import Tensor

from kohakutpu.application import ktpugrad
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import device as card
from kohakutpu.application.runtime import forms as FM

#: ViT-B's machine: K blocks paired (nk=2), the packing its FC1 output takes.
BOARD = "multimesh_v9t6"
R = np.random.default_rng(0)


@pytest.fixture
def dev():
    ktpugrad.install()
    d = card.Device(kind="double", board=BOARD, nodes=1)
    with card.attached(d):
        yield d
    d.close()


def tensor(x):
    return Tensor(x.astype(np.float16), device="KTPU").realize()


def storage(t: Tensor):
    return FM.base_of(cast(Buffer, t.uop.buffer)._buf)[0]


def kinds(dev, run):
    """The kinds of the launches `run` submits, and the L2 of its products."""
    seen, texts = [], []
    submit = dev.run

    def spied(compiled, bindings, kind, info):
        seen.append(kind)
        if kind == "matmul":
            texts.append(compiled.l2_text)
        return submit(compiled, bindings, kind, info)

    dev.run = spied
    try:
        got = run()
    finally:
        dev.run = submit
    return got, seen, texts


def operands():
    x = tensor(R.normal(size=(37, 64)))
    w1, b1 = tensor(R.normal(size=(64, 128))), tensor(R.normal(size=128))
    return x, w1, b1, tensor(R.normal(size=(128, 64)) / 8)


def form(dev, st, key) -> bytes:
    return dev.read(st.forms[key].addr, key[5] * key[6])


#: A B packing of the [37, 128] activation as the quant writes it: rows to 64,
#: one packed tile of them (a consumer's N tile of 64).
B_KEY = ("b", 37, 128, 64, 128, 16, 2)


def b_packing(dev, st):
    """The activation packed as a consumer's B (`forms.packed`): its form key."""
    side, rows, cols, rp, cp, g, nk = B_KEY
    src = Operand("full", pitch=cols)
    FM.packed(dev, st, np.float16, src, rows, cols, rp, cp, side, g, nk)
    return ("mx7", side, src, rows, cols, rp, cp, g, nk, 1)


def test_the_second_call_packs_in_the_epilogue_and_launches_no_quant(dev):
    """Rows padded 37 -> 40, K blocks paired: the consumer's packing is the
    product's epilogue's from the second call, byte for byte the quant's."""
    x, w1, b1, w2 = operands()
    calls = []
    for _ in range(2):
        h = (x @ w1 + b1).gelu().realize()
        got, seen, texts = kinds(dev, lambda h=h: (h @ w2).numpy())
        calls.append((h, got, seen, texts))
    (key,) = next(iter(FM.demands(dev).values()))
    assert (key[1], key[5], key[8]) == ("a", 40, 2), key
    assert "quant" in calls[0][2], calls[0][2]
    assert "quant" not in calls[1][2], calls[1][2]
    first, second = storage(calls[0][0]), storage(calls[1][0])
    assert key in second.valid
    assert form(dev, first, key) == form(dev, second, key)
    assert calls[0][1].tobytes() == calls[1][1].tobytes()


def test_the_product_kernel_stores_both_outputs_checked(dev):
    x, w1, b1, w2 = operands()
    texts = []
    for _ in range(2):
        _, _, got = kinds(dev, lambda: ((x @ w1 + b1).gelu() @ w2).numpy())
        texts.append(got[0])
    assert "store q0[" not in texts[0], texts[0]
    assert "store out[" in texts[1] and "store q0[" in texts[1], texts[1]
    assert "status=" in texts[1] and "quant_check" in texts[1], texts[1]


def test_a_product_with_two_consumer_packings_writes_both(dev):
    """The activation read as an A and as a B (rows past the epilogue's trips
    zero): both written by the epilogue, each byte-equal to its quant."""
    x, w1, b1, w2 = operands()
    calls = []
    for _ in range(2):
        h = (x @ w1 + b1).gelu().realize()

        def step(h=h):
            return (h @ w2).numpy(), b_packing(dev, storage(h))

        _, seen, _ = kinds(dev, step)
        calls.append((h, seen))
    keys = list(next(iter(FM.demands(dev).values())))
    assert sorted(k[1] for k in keys) == ["a", "b"], keys
    assert "quant" not in calls[1][1], calls[1][1]
    first, second = storage(calls[0][0]), storage(calls[1][0])
    for key in keys:
        assert key in second.valid, key
        assert form(dev, first, key) == form(dev, second, key), key


def test_a_nan_in_the_epilogue_fails_the_product(dev):
    """A NaN bias makes the epilogue's values NaN: its checked packing fails the
    product, the consumer's quant no longer running to catch it."""
    x, w1, b1, w2 = operands()
    nan = tensor(np.where(np.arange(128) == 5, np.nan, 1.0))

    def step(b):
        return ((x @ w1 + b).gelu() @ w2).numpy()

    step(b1)
    _, seen, texts = kinds(dev, lambda: step(b1))
    assert "quant" not in seen and "store q0[" in texts[0], seen
    with pytest.raises(Exception, match="non-finite"):
        step(nan)
