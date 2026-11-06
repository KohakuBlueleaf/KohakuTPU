"""Producer failures release acquired outputs without releasing caller storage."""

import importlib
from types import SimpleNamespace

import numpy as np
import pytest

from kohakutpu.application.api import custom
from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import forms as F
from kohakutpu.application.runtime import rows as R
from kohakutpu.application.runtime.memory import storage as S
from kohakutpu.language.dsl.types import TensorSpec

T = importlib.import_module("kohakutpu.application.api.tensor")
C = importlib.import_module("kohakutpu.application.runtime.call")
A = importlib.import_module("kohakutpu.application.api.attention")


class Escape(BaseException):
    pass


def fail(*args, **kw):
    raise Escape()


class Kernel:
    name = "owned"

    def __init__(self, inputs: list[str], outputs: list[str]):
        self.inputs = inputs
        self.outputs = outputs


class Memory:
    _recipe_recording = None

    def specialise(self, *args, **kw):
        return SimpleNamespace(params=self.params)

    def run(self, *args, **kw):
        pass

    def __init__(self, fail_at=None):
        self.params: list[tuple[str, TensorSpec]] = []
        self.live = set()
        self.next = 0
        self.fail_at = fail_at
        self.tables = {}
        self.constants = set()
        self.machine = SimpleNamespace(count=lambda kind: 1)
        self.mmu = SimpleNamespace(
            resolve=lambda at: (SimpleNamespace(arena=self, physical=at), at)
        )

    def alloc(self, size):
        self.next += 1
        if self.next == self.fail_at:
            raise Escape()
        self.live.add(self.next)
        return self.next

    def free(self, at):
        self.live.remove(at)

    def write(self, *args):
        pass


@pytest.mark.parametrize("fault", ["spec", "allocation", "run", "wrapper"])
def test_custom_outputs_transfer_only_after_complete_construction(monkeypatch, fault):
    dev = Memory(fail_at=3 if fault == "allocation" else None)
    dev.alloc(32)
    baseline = set(dev.live)
    specs = [TensorSpec((16,), "f16"), TensorSpec((16,), "f16")]
    if fault == "spec":
        specs[1] = TensorSpec((16,), "f16", (("tile", (4, 4)),))
    kernel = Kernel([], ["a", "b"])
    dev.params = list(zip(kernel.outputs, specs))
    if fault == "run":
        monkeypatch.setattr(dev, "run", fail)
    monkeypatch.setattr(custom.D, "device", lambda: dev)
    if fault == "wrapper":
        tensor = custom.Tensor
        made = 0

        def wrapper(*args):
            nonlocal made
            made += 1
            if made == 2:
                fail()
            return tensor(*args)

        monkeypatch.setattr(custom, "Tensor", wrapper)
    error = T.Unsupported if fault == "spec" else Escape
    with pytest.raises(error):
        custom.call(kernel)
    assert dev.live == baseline


def test_upload_write_failure_releases_storage():
    dev = Memory()
    dev.alloc(32)
    baseline = set(dev.live)
    dev.write = fail
    arr = np.arange(16, dtype=np.float16)
    kernel = Kernel(["x"], [])
    dev.params = [("x", TensorSpec((16,), "f16"))]
    with pytest.raises(Escape):
        C.call(dev, kernel, {"x": arr})
    assert dev.live == baseline


@pytest.mark.parametrize("fault", ["write", "resolve", "publish"])
def test_table_failure_never_publishes_unowned_storage(fault):
    dev = Memory()
    if fault == "write":
        dev.write = fail
    elif fault == "resolve":
        dev.mmu.resolve = fail
    else:

        class Refuse(dict):
            def __setitem__(self, key, value):
                raise Escape()

        dev.tables = Refuse()
    with pytest.raises(Escape):
        F.table(dev, np.arange(16, dtype=np.float16))
    assert not dev.live
    assert not dev.tables
    assert not dev.constants


@pytest.mark.parametrize("producer", ["pending", "relayouts"])
@pytest.mark.parametrize("fault", ["allocation", "relayout"])
def test_relayout_temporaries_are_owned_during_production(monkeypatch, producer, fault):
    dev = Memory(fail_at=2 if fault == "allocation" else None)
    steps = tuple(
        SimpleNamespace(src=0, n=16, walk=(), at=0, dwalk=(), m=16, dat=None)
        for _ in range(3)
    )
    calls = 0

    def relayout(*args):
        nonlocal calls
        calls += 1
        if fault == "relayout" and calls == 2:
            fail()

    monkeypatch.setattr(F, "relayout", relayout)
    pending = object.__new__(F.Pending)
    pending.storage, pending.dev, pending.steps, pending.nbytes = None, dev, steps, 32
    with pytest.raises(Escape):
        if producer == "pending":
            pending.made()
        else:
            F._relayouts(dev, steps, 999)
    assert not dev.live
    assert pending.storage is None


def test_partial_reduction_second_allocation_releases_first(monkeypatch):
    dev = Memory(fail_at=2)
    monkeypatch.setattr(R, "single_package_enabled", lambda: False)
    monkeypatch.setattr(
        F.generated, "rows_shape", lambda *args: {"nblk": 2, "out": (2, 16)}
    )
    with pytest.raises(Escape):
        R.run(
            dev,
            X.Reduce("sum", X.Load(0)),
            [Operand("full", pitch=64)],
            {"in0": 999},
            4,
            64,
            Operand("row"),
            1000,
        )
    assert not dev.live


@pytest.mark.parametrize("producer", ["wide", "flat", "statistic", "planned", "matmul"])
def test_tensor_output_cleanup_is_independent_of_error_translation(
    monkeypatch, producer
):
    dev = Memory()
    t = SimpleNamespace(size=128, shape=(4, 32), rows=(4, 32), _stat=False)
    monkeypatch.setattr(T.RW, "run", fail)
    monkeypatch.setattr(T.FM.generated, "flat", fail)
    monkeypatch.setattr(T.R, "execute", fail)
    monkeypatch.setattr(T, "_access", lambda *args, **kw: None)
    with pytest.raises(Escape):
        if producer in ("wide", "flat"):
            T._wide(dev, t, 1.0, [], (4, 17) if producer == "flat" else (4, 32), {})
        elif producer == "statistic":
            T._statistic(dev, t, 1.0, [], (4, 32), {})
        elif producer == "planned":
            T._planned_rows(dev, t, 1.0, [], (4, 32), {})
        else:
            a = SimpleNamespace(shape=(4, 32), st=object(), ndim=2)
            T._matmul(dev, t, SimpleNamespace(a=a, b=a), 1.0, [], (4, 32), {})
    assert not dev.live


@pytest.mark.parametrize("fault", ["allocation", "launch", "wrapper"])
def test_attention_owns_outputs_until_wrapper_publication(monkeypatch, fault):
    dev = Memory(fail_at=2 if fault == "allocation" else None)
    operand = SimpleNamespace(
        shape=(1, 64, 64),
        ndim=3,
        dtype=np.float16,
        st=SimpleNamespace(dev=dev),
        realize=lambda: None,
    )
    monkeypatch.setattr(A.F, "packed", lambda *args: 999)
    monkeypatch.setattr(A.F, "table", lambda *args: 999)
    monkeypatch.setattr(A.A, "kernel", lambda *args, **kw: None)
    monkeypatch.setattr(
        A.F, "launch", fail if fault == "launch" else lambda *args: None
    )
    monkeypatch.setattr(A.F, "relayout", lambda *args: None)
    if fault == "wrapper":
        monkeypatch.setattr(A, "Tensor", fail)
    with pytest.raises(Escape):
        A.attention(operand, operand, operand)
    assert not dev.live


def test_tensor_wrapper_failure_does_not_publish_or_leak_output(monkeypatch):
    dev = Memory()
    tensor = T.Tensor((4, 32), node=T.Node(1.0, (4, 32)))
    monkeypatch.setattr(T.D, "device", lambda: dev)
    monkeypatch.setattr(T, "_wide", lambda *args: T.Storage(dev, 256))
    monkeypatch.setattr(T, "Buf", fail)
    with pytest.raises(Escape):
        T._realize(tensor)
    assert not dev.live
    assert tensor._buf is None
    assert tensor.lazy


def test_storage_constructor_failure_preserves_allocation_owner(monkeypatch):
    dev = Memory()
    at = None
    baseline = set(dev.live)
    monkeypatch.setattr(S, "Form", fail)
    with pytest.raises(Escape):
        S.Storage(dev, 32, addr=at)
    assert dev.live == baseline


def test_cache_insertion_failure_does_not_take_holder_reference():
    dev = Memory()
    dev._recipe_recording = None
    source, prior, output = (S.Storage(dev, 32) for _ in range(3))
    source.keep("read", prior)
    previous = source.kept["read"]

    class RefusingCache(dict):
        def __setitem__(self, key, value):
            raise Escape()

    source.kept = RefusingCache(source.kept)
    with pytest.raises(Escape):
        source.keep("read", output)
    assert source.kept["read"] is previous
    output.free()
    assert dev.live == {source.addr, prior.addr}
    source.free()
    prior.free()
    assert not dev.live
