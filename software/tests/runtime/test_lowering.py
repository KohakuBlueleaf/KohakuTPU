"""Every kernel shape a model plans compiles to packages: the double stops at L2."""

import numpy as np
import pytest
from tinygrad.tensor import Tensor

from kohakutpu.application import ktpugrad
from kohakutpu.application.runtime import device as card

BOARD = "multimesh_v9t8"
R = np.random.default_rng(2)


@pytest.fixture
def built():
    ktpugrad.install()
    d = card.Device(kind="offline", board=BOARD, nodes=1)
    with card.attached(d):
        yield d
    d.close()


def tensor(*shape):
    return Tensor(R.normal(size=shape).astype(np.float16), device="KTPU").realize()


@pytest.mark.parametrize(
    "shape",
    [
        (1, 128),
        (1, 256),
        (1, 512),
        (8, 128),
        (32, 192),
        (64, 1024),
        (4, 7, 64),
        (1, 100352),
    ],
)
def test_elementwise_rows(built, shape):
    x = tensor(*shape)
    (x * 2 + x.exp()).realize()


@pytest.mark.parametrize("shape", [(1, 256), (8, 192), (64, 512)])
def test_row_reductions(built, shape):
    x = tensor(*shape)
    x.softmax(-1).realize()
    (x - x.mean(-1, keepdim=True)).realize()
    x.sum(-1).realize()
    x.sum(0).realize()


@pytest.mark.parametrize(
    "n,k,m", [(1, 256, 1024), (16, 192, 192), (64, 1024, 10), (800, 32, 160)]
)
def test_products_and_epilogues(built, n, k, m):
    x, w = tensor(n, k), tensor(k, m)
    (x @ w).realize()
    ((x @ w).relu() + 1).realize()


def test_a_column_reduction_past_the_staging_room(built):
    x, y = tensor(16, 65536), tensor(1, 65536)
    (x * y).sum(0).realize()


def test_layout_changes(built):
    x = tensor(32, 96)
    x.permute(1, 0).contiguous().realize()
    tensor(784, 128).permute(1, 0).contiguous().realize()
    tensor(3, 8, 16, 32).permute(0, 2, 1, 3).contiguous().realize()
