"""One witness per model/recipe contract; no card or Verilator model."""

from itertools import islice
from types import SimpleNamespace

import numpy as np
import pytest
from examples.tinygrad.mnist_mlp import (
    HostLearningRate,
    epoch_batches,
    evaluate,
    learning_rate,
)
from examples.tinygrad.mnist_models import ConvNet, LeNet
from tinygrad import TinyJit, nn
from tinygrad.helpers import Context, GlobalCounters
from tinygrad.nn.state import get_state_dict
from tinygrad.tensor import Tensor

from kohakutpu.application import ktpugrad
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import forms as FM


@pytest.fixture
def cpu():
    ktpugrad.uninstall()
    with Context(DEV="CPU"):
        yield
    ktpugrad.install()


class OriginalLeNet:
    def __init__(self):
        self.c1 = nn.Conv2d(1, 6, 5, padding=2)
        self.g1 = nn.GroupNorm(2, 6)
        self.c2 = nn.Conv2d(6, 16, 5)
        self.g2 = nn.GroupNorm(4, 16)
        self.l1 = nn.Linear(16 * 5 * 5, 120)
        self.n1 = nn.LayerNorm(120)
        self.l2 = nn.Linear(120, 84)
        self.l3 = nn.Linear(84, 10)

    def __call__(self, x):
        x = x.reshape(-1, 1, 28, 28)
        x = self.g1(self.c1(x)).silu().avg_pool2d((2, 2))
        x = self.g2(self.c2(x)).silu().avg_pool2d((2, 2))
        x = self.n1(self.l1(x.flatten(1))).silu()
        return self.l3(self.l2(x).silu())


def test_lenet_preserves_parameters_and_forward(cpu):
    Tensor.manual_seed(0)
    original = OriginalLeNet()
    Tensor.manual_seed(0)
    configured = LeNet()
    a, b = get_state_dict(original), get_state_dict(configured)
    assert list(a) == list(b)
    for name in a:
        np.testing.assert_array_equal(a[name].numpy(), b[name].numpy())
    x = Tensor(
        np.random.default_rng(5).normal(size=(2, 784)).astype(np.float32), device="CPU"
    )
    np.testing.assert_array_equal(original(x).numpy(), configured(x).numpy())


def test_nonpreset_config_projection_and_batch_stats(cpu):
    model = ConvNet(
        channels=(3, 5),
        kernels=(3, 1),
        strides=(2, 1),
        pool=(0, 2),
        norm="batch",
        residual=True,
        activation="relu",
        head=(7,),
    )
    with Context(TRAINING=1):
        model(Tensor.ones(2, 784, device="CPU")).realize()
        model.g1.running_mean.realize()
        model.g1.running_var.realize()
        model.g1.num_batches_tracked.realize()
    assert model.r1.weight.shape == (3, 1, 1, 1)
    assert model.l1.weight.shape == (7, 5 * 7 * 7)
    assert model.g1.num_batches_tracked.item() == 1
    assert np.any(model.g1.running_mean.numpy() != 0)
    with Context(TRAINING=0):
        assert model(Tensor.ones(2, 784, device="CPU")).numpy().shape == (2, 10)
    assert model.g1.num_batches_tracked.item() == 1


def test_host_lr_changes_existing_buffer_under_jit(cpu):
    opt = SimpleNamespace(lr=Tensor([0.1], device="CPU"))
    writer = HostLearningRate(opt)
    buffer = writer.buffer
    run = TinyJit(lambda x: (x * opt.lr).realize())
    observed = []
    for step in range(6):
        kernels = GlobalCounters.kernel_count
        value = writer.write(learning_rate(step, 6, 0.1, "cosine", 1, 0.01))
        assert GlobalCounters.kernel_count == kernels
        assert opt.lr.uop.buffer is buffer
        assert writer.read() == value
        observed.append(float(run(Tensor([2.0], device="CPU").realize()).item()))
        np.testing.assert_allclose(observed[-1], 2 * value)
    assert observed[-1] < observed[0]
    assert learning_rate(5, 6, 0.1, "onecycle", lr_min=0.01) == 0.01


def test_epoch_tail_and_full_evaluation(cpu):
    batches = list(islice(epoch_batches(11, 4), 6))
    assert [len(x) for x in batches] == [4, 4, 3, 4, 4, 3]
    np.testing.assert_array_equal(np.sort(np.concatenate(batches[:3])), np.arange(11))
    np.testing.assert_array_equal(np.sort(np.concatenate(batches[3:])), np.arange(11))
    shapes = []

    def model(x):
        shapes.append(x.shape)
        return x[:, :10]

    data = np.zeros((11, 784), dtype=np.float32)
    data[:, 0] = 1
    result = evaluate(model, data, np.zeros(11, dtype=np.int32), "CPU", 4)
    assert result["images"] == 11 and result["accuracy"] == 1
    assert shapes == [(4, 784)] * 3


def test_host_upload_refreshes_side_form_before_recording():
    dev = D.attach(D.Device(kind="offline", board="multimesh_v9t6"))
    opt = SimpleNamespace(lr=Tensor(np.array([0.002], dtype=np.float32), device="KTPU"))
    writer = HostLearningRate(opt)
    st = writer.buffer._buf
    address = FM.f16(dev, st, np.float32, 1)
    source_dtype = st.host_f16_dtype
    original_runs = dev.counters["runs"]

    def witness(value):
        writer.write(value)
        assert FM.F16 in st.valid
        np.testing.assert_array_equal(
            np.frombuffer(dev.read(address, 2), np.float16),
            np.array([value], np.float16),
        )

    witness(0.001)
    # Removing the producer metadata is the mutant: copyin no longer refreshes.
    st.host_f16_dtype = None
    with pytest.raises(AssertionError):
        witness(0.0005)
    st.host_f16_dtype = source_dtype
    witness(0.0005)
    assert dev.counters["runs"] == original_runs
