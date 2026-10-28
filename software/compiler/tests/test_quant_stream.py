"""Stream reordering preserves the paired address domain, not its visitation order."""

import json
import re

import pytest

from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def compile_stream(
    *, axes=0, fill=0, mutant=None, shape=(6, 12544), padded=(32, 12544), groups=8, nk=2
):
    source = f"""target ktpu.v9
fn stream.l2(x: f16[{shape[0]},{shape[1]}] @dram, q: mx7b[{padded[0]},{padded[1]}] @dram(gn={groups},nk={nk}))
    quant q <- x pad=1 finite_batch=4 overlap=1 pipeline=1 zero_classifier=1 stream_axes={axes} fill_overlap={fill}
"""
    machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    result = rs._rs.compile(
        source, rs.machine_json(machine), cache_dir="", fetch=True, mutant=mutant
    )
    assert not result["verdict"]["refused"], result["verdict"]
    return result


def test_stream_axes_reverse_witness():
    baseline = compile_stream()
    result = compile_stream(axes=1)

    def witness(result):
        assert (
            result["l1"].count("        run ")
            < baseline["l1"].count("        run ") // 4
        )
        assert "ainc a0 += 896" in result["l1"]

    witness(result)
    undone = compile_stream(axes=1, mutant="l2.packing/ordered-axes")
    assert undone["l1"] == baseline["l1"]
    with pytest.raises(AssertionError):
        witness(undone)


def test_fill_overlap_reverse_witness():
    baseline = compile_stream(axes=1)
    result = compile_stream(axes=1, fill=1)

    def witness(result):
        assert "vsync fe on=fill" not in result["l1"]
        assert "vsync unpack on=fill" in result["l1"]
        assert "vsync fe on=unpack" in result["l1"]
        assert "vsync drain on=pack" in result["l1"]

    witness(result)
    undone = compile_stream(axes=1, fill=1, mutant="l2.packing/frontend-fill")
    assert undone["l1"] == baseline["l1"]
    with pytest.raises(AssertionError):
        witness(undone)


@pytest.mark.parametrize(
    "shape,padded,groups,nk",
    [
        ((6, 128), (32, 128), 8, 2),
        ((128, 64), (128, 64), 8, 2),
        ((13, 48), (16, 64), 4, 1),
        ((8, 65536), (8, 65536), 2, 1),
    ],
)
def test_stream_axes_compile_partial_contiguous_and_large_pitch(
    shape, padded, groups, nk
):
    result = compile_stream(
        axes=1, fill=1, shape=shape, padded=padded, groups=groups, nk=nk
    )
    assert "assert_zero" in result["l1"]
    assert re.search(r"vpack v\d+ -> l1\[\d+\] mx7b", result["l1"])
    assert "vsync unpack on=fill" in result["l1"]


def test_quant_default_and_stream_key_admission(monkeypatch):
    for name in (
        "KTPU_QUANT_PIPELINE",
        "KTPU_QUANT_ZERO_CLASSIFIER",
        "KTPU_QUANT_STREAM_AXES",
        "KTPU_QUANT_FILL_OVERLAP",
    ):
        monkeypatch.delenv(name, raising=False)
    machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    keys = []
    for axes, fill in ((0, 0), (1, 0), (0, 1), (1, 1)):
        monkeypatch.setenv("KTPU_QUANT_STREAM_AXES", str(axes))
        monkeypatch.setenv("KTPU_QUANT_FILL_OVERLAP", str(fill))
        kernel, given = MM.quant_kernel(
            V.Operand("full", pitch=128), 6, 128, 32, 128, "b", 8, 2
        )
        graph = kernel.trace(given, machine=K.dsl_machine(machine)).graph
        source = json.dumps(graph)
        compiled = rs._rs.compile(
            source, rs.machine_json(machine), cache_dir="", fetch=True
        )
        assert not compiled["verdict"]["refused"], compiled["verdict"]
        assert "vcmplt " not in compiled["l1"]
        assert "vsync drain on=pack" in compiled["l1"]
        keys.append(
            json.loads(rs._rs.library_key(source, rs.machine_json(machine)))["class"]
        )
    assert len(set(keys)) == 4
