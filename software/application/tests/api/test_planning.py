"""Tier-0 API product plans, padded forms and clause identifiers; no card."""

import inspect
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from kohakutpu.application import api as kp
from kohakutpu.application.kernels import convert, lanes, matmul, transpose, vector
from kohakutpu.application.kernels import expr as X
from kohakutpu.application.ktpugrad import reference as REF
from kohakutpu.application.ktpugrad import run as R
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime import rows as RW
from kohakutpu.language import machine as MF


@pytest.fixture
def offline():
    with D.attached(D.Device(kind="offline", board="multimesh_v9t6")) as dev:
        yield dev


@pytest.mark.parametrize("n", [1, 10, 32, 48, 65])
@pytest.mark.parametrize("m", [1, 5, 32])
def test_product_pads_n_through_device_planner(offline, m, n):
    a = kp.tensor(np.ones((m, 64), np.float16))
    b = kp.tensor(np.ones((n, 64), np.float16))
    y = (a @ b.T).realize()
    machine = MF.load(offline.machine.board)
    width = machine.vc.vlmax // machine.mg.lanes
    np_ = -(-n // width) * width
    runs = [r for r in offline.runs if r.kind == "matmul"]
    assert len(runs) == 1
    mm = runs[0].info["mm"]
    assert (mm.m, mm.n, mm.k) == (m, np_, 64)
    assert y.shape == (m, n)
    assert y.st.nbytes == 2 * m * n
    key = FM.padded(y.st)
    if n % 16:
        assert key == FM.rows_key(n, np_)
        assert y.st.valid == {key}
        # A poisoned padding distinguishes compact read-back from raw bytes.
        padded = np.full((m, np_), -999, np.float16)
        expected = np.arange(m * n, dtype=np.float16).reshape(m, n)
        padded[:, :n] = expected
        offline.write(y.st.forms[key].addr, padded.tobytes())
        np.testing.assert_array_equal(y.numpy(), expected)
    else:
        assert ("flat",) in y.st.valid


def test_padded_product_can_be_consumed(offline, monkeypatch):
    y = (kp.tensor(np.ones((5, 64))) @ kp.tensor(np.ones((10, 64))).T).realize()
    # RUL.028/029: consumers read the producer's recorded padded pitch.
    key = FM.padded(y.st)
    assert key is not None
    values = np.arange(50, dtype=np.float16).reshape(5, 10)
    padded = np.full((5, key[2]), -999, np.float16)
    padded[:, :10] = values
    offline.write(y.st.forms[key].addr, padded.tobytes())
    offline.write(y.st.addr, np.full((5, 10), -777, np.float16).tobytes())
    original, consumed = RW.run, []

    def observe(dev, e, operands, binds, rows, width, out, out_at, info=None):
        # Offline compiles only: witness the actual bound reads and expression.
        assert rows == 5
        (operand,) = operands
        assert operand.kind == "full"
        reads = np.stack(
            [
                np.frombuffer(
                    dev.read(
                        binds["in0"] + 2 * (operand.offset + r * operand.pitch),
                        2 * 10,
                    ),
                    np.float16,
                )
                for r in range(rows)
            ]
        )
        consumed.append(X.evaluate(e, [reads]))
        return original(dev, e, operands, binds, rows, width, out, out_at, info)

    monkeypatch.setattr(RW, "run", observe)
    z = (y + 1).realize()
    assert z.shape == (5, 10)
    assert y.st.nbytes == z.st.nbytes == 100
    assert len(consumed) == 1
    np.testing.assert_array_equal(consumed[0], values + 1)


@pytest.mark.parametrize("shared", [False, True])
def test_batched_padded_product_and_epilogue(offline, shared):
    a = kp.tensor(np.ones((2, 5, 64), np.float16))
    b = kp.tensor(np.ones((10, 64) if shared else (2, 10, 64), np.float16))
    bias = kp.tensor(np.ones((10,), np.float16))
    y = kp.linear(a, b, bias).relu().realize()
    mm = next(r.info["mm"] for r in offline.runs if r.kind == "matmul")
    assert (mm.bt, mm.m, mm.n, mm.shared) == (2, 5, 32, shared)
    assert y.shape == (2, 5, 10)
    assert y.st.valid == {FM.rows_key(10, 32)}


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("dtype", [np.float16, np.float32])
def test_product_adapter_preserves_values(offline, monkeypatch, shared, dtype):
    # Check the API-to-plan mapping by its numpy meaning, without a kernel run.
    monkeypatch.setattr(R, "execute", REF.execute)
    rng = np.random.default_rng(28)
    a = rng.normal(size=(2, 3, 5, 16)).astype(dtype)
    b = rng.normal(size=(10, 16) if shared else (2, 3, 10, 16)).astype(dtype)
    bias = rng.normal(size=(10,)).astype(dtype)
    got = (
        kp.linear(kp.tensor(a, dtype), kp.tensor(b, dtype), kp.tensor(bias, dtype))
        .relu()
        .numpy()
    )
    want = np.maximum(
        a.astype(np.float64) @ b.astype(np.float64).swapaxes(-1, -2) + bias, 0
    ).astype(np.float16)
    np.testing.assert_array_equal(got, want)


def test_product_column_block_is_read_from_schema(offline, monkeypatch):
    original = MF.load

    def load(name):
        m = original(name)
        data = {**m.dict(), "vc": {**m.vc.dict(), "vlmax": 256, "chunks": 16}}
        return MF.Machine(data)

    monkeypatch.setattr(MF, "load", load)
    y = (kp.tensor(np.ones((4, 64))) @ kp.tensor(np.ones((10, 64))).T).realize()
    assert y.st.valid == {FM.rows_key(10, 64)}
    assert next(r.info["mm"].n for r in offline.runs if r.kind == "matmul") == 64


@pytest.mark.parametrize(
    "make, clause",
    [
        (lambda: kp.Tensor((2,), node=SimpleNamespace()).st, "API.REF.008"),
        (lambda: kp.tensor([1]).T, "API.REF.009"),
        (lambda: kp.tensor(np.ones((2, 2))).T.reshape(4), "API.REF.009"),
        (lambda: bool(kp.tensor([1])), "API.REF.010"),
        (lambda: kp.tensor([1], dtype=np.int32), "API.REF.011"),
        (lambda: kp.tensor(np.ones((2, 3))) + kp.tensor(np.ones((4,))), "API.REF.012"),
        (lambda: kp.tensor(np.ones((2, 64))).T + 1, "API.REF.005"),
        (lambda: kp.tensor(np.ones((2, 64))).sum(-1).sum(), "API.REF.006"),
        (
            lambda: kp.tensor(np.ones((2, 64))) @ kp.tensor(np.ones((3, 32))).T,
            "API.REF.004",
        ),
        (
            lambda: (
                kp.tensor(np.ones((2, 2, 64))) + kp.tensor(np.ones((2, 1, 64)))
            ).realize(),
            "API.REF.013",
        ),
    ],
)
def test_refusal_has_clause(offline, make, clause):
    with pytest.raises(kp.Unsupported) as caught:
        make()
    assert caught.value.clause == clause
    assert clause in str(caught.value)


def test_generated_refusal_has_clause_and_frees_output(offline, monkeypatch):
    # API.REF.007: eight full inputs cannot fit a 16-row statistic trip in L1.
    inputs = [kp.tensor(np.ones((2, 32))) for _ in range(8)]
    y = sum(inputs).sum(-1)
    live = dict(offline.dram.live)
    alloc = Mock(wraps=offline.alloc)
    free = Mock(wraps=offline.free)
    monkeypatch.setattr(offline, "alloc", alloc)
    monkeypatch.setattr(offline, "free", free)

    with pytest.raises(kp.Unsupported) as caught:
        y.realize()

    assert caught.value.clause == "API.REF.007"
    assert "API.REF.007" in str(caught.value)
    assert isinstance(caught.value.__context__, ValueError)
    alloc.assert_called_once_with(32)
    free.assert_called_once()
    assert offline.dram.live == live
    assert y.lazy


@pytest.mark.parametrize(
    "generator, parameters",
    [
        (vector.rows_kernel, ("cores",)),
        (vector.flat_kernel, ("cores",)),
        (matmul.mm_kernel, ("cores", "clusters")),
        (convert.cast_kernel, ("cores",)),
        (lanes.relayout_kernel, ("cores",)),
        (transpose.transpose_kernel, ("cores",)),
        (transpose.reverse_kernel, ("cores",)),
    ],
)
def test_generators_require_machine_counts(generator, parameters):
    signature = inspect.signature(generator)
    for name in parameters:
        assert signature.parameters[name].default is inspect.Parameter.empty
