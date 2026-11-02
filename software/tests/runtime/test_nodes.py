"""Kernels placed on a node: requests that overlap, batches split across nodes."""

import time

import numpy as np
import pytest
from tinygrad.tensor import Tensor

from kohakutpu.application import ktpugrad
from kohakutpu.application.ktpugrad import Resident, Sharded, shards
from kohakutpu.application.runtime import device as card
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime.device.common import Submitted
from kohakutpu.stack.rs import _rs

R = np.random.default_rng(1)
BOUND = 0.05
BOARD = "multimesh_v9t8"


def two_nodes(kind):
    ktpugrad.install()
    d = card.Device(kind=kind, board=BOARD, nodes=2)
    with card.attached(d):
        yield d
    d.close()


@pytest.fixture
def two():
    yield from two_nodes("double")


@pytest.fixture
def built():
    """Real packages, nothing run."""
    yield from two_nodes("offline")


def close(got, want):
    assert np.abs(got.astype(np.float64) - want).max() <= BOUND * np.abs(want).max()


def tensor(x):
    return Tensor(x.astype(np.float16), device="KTPU").realize()


def test_a_kernel_in_a_node_scope_is_that_nodes_package(built):
    x, w = tensor(R.normal(size=(16, 32))), tensor(R.normal(size=(32, 32)))
    seen, run = [], built.card.run

    def spied(program):
        seen.append(program)
        return run(program)

    built.card.run = spied
    with built.on_node(1):
        (x @ w).realize()
    assert seen and all(list(program) == [1] for program in seen)
    meshes = set()
    for program in seen:
        tables = _rs.package_read(program[1].package, built.card.machine_json, 1)
        meshes |= {u["mesh"] for u in (*tables["units"], *tables["buffers"])}
    assert meshes == {1}


def test_each_node_has_its_own_scratch_window(two):
    spans = [("dram", 64, "dram", None, ()), ("stage", 64, "stage", None, ())]
    first = two._windowed(spans)
    with two.on_node(1):
        second = two._windowed(spans)
    assert first["dram"] != second["dram"] and first["stage"] != second["stage"]
    assert set(two.windows) == {
        ("dram", None),
        ("stage", 0),
        ("dram", 1),
        ("stage", 1),
    }


def in_flight(dev, node, span, wrote):
    refs = dev._retain([span])
    out = Submitted(
        "held",
        "dsl",
        {},
        time.monotonic(),
        references=refs,
        writes=list(refs) if wrote else [],
        nodes=(node,),
    )
    out.result = {"cycles": 0}
    dev.inflight.append(out)
    return out


def test_a_run_waits_only_for_another_nodes_shared_write(two):
    two.node_overlap = True
    shared, other = two.alloc(64), two.alloc(64)
    ref = two.mmu.resolve(shared)[0]
    refs = [(ref.arena, ref.physical)]
    in_flight(two, 0, shared, wrote=True)
    two._fence((1,), [], [])
    two._fence((0,), refs, refs)
    assert len(two.inflight) == 1
    two._fence((1,), refs, [])
    assert not two.inflight
    in_flight(two, 0, shared, wrote=False)
    two._fence((1,), refs, [])
    assert len(two.inflight) == 1
    two.drain()
    two.free(other)


def test_a_constant_table_is_no_runs_write(two):
    at = FM.table(two, np.arange(8.0))
    ref = two.mmu.resolve(at)[0]
    refs = [(ref.arena, ref.physical)]
    assert two._write_refs(refs) == []
    with two.submission_scope("region"):
        assert two._write_refs(refs) == []


def test_without_overlap_a_run_waits_for_every_other_node(two):
    two.node_overlap = False
    in_flight(two, 0, two.alloc(64), wrote=False)
    two._fence((1,), [], [])
    assert not two.inflight


def test_requests_on_two_nodes_each_get_their_answer(two):
    b = R.normal(size=(32, 32))
    w = tensor(b)
    model = Resident(lambda x: x @ w)
    inputs = [R.normal(size=(16, 32)) for _ in range(2)]
    for _ in range(3):
        outs = model([(tensor(a),) for a in inputs])
        for out, a in zip(outs, inputs, strict=True):
            close(out.numpy(), a @ b)


def test_more_requests_than_nodes_each_keep_their_answer(two):
    b = R.normal(size=(32, 32))
    w = tensor(b)
    model = Resident(lambda x: x @ w)
    inputs = [R.normal(size=(16, 32)) for _ in range(5)]
    model([(tensor(inputs[0]),)] * 2)
    outs = model([(tensor(a),) for a in inputs])
    for out, a in zip(outs, inputs, strict=True):
        close(out.numpy(), a @ b)


def test_a_batch_splits_by_range_across_the_nodes(two):
    a, b = R.normal(size=(7, 32)), R.normal(size=(32, 32))
    w = tensor(b)
    close(Sharded(lambda x: x @ w)(tensor(a)).numpy(), a @ b)


def test_ranges_cover_the_batch_in_order():
    assert shards(7, [0, 1, 2]) == [(0, 0, 3), (1, 3, 5), (2, 5, 7)]
    assert shards(2, [0, 1, 2, 3]) == [(0, 0, 1), (1, 1, 2)]
