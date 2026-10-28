"""Finite MX7 batches retain byte maps while amortizing front-end barriers."""

import json
import re

import pytest

from kohakutpu.application.kernels import matmul
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def compile_batch(
    batch,
    *,
    mutant=None,
    checked=False,
    classifier_overlap=False,
    pipeline=False,
    zero_classifier=False,
):
    machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    source = (
        "target ktpu.v9\n"
        "fn q.l2(x: f16[64,32] @dram, q: mx7a[64,32] @dram(gm=16,nk=1))\n"
        f"    quant q <- x finite_batch={batch} overlap=1"
        + (" classifier_overlap=1" if classifier_overlap else "")
        + (" pipeline=1" if pipeline else "")
        + (" zero_classifier=1" if zero_classifier else "")
        + ("\n" if checked else " finite=1\n")
    )
    return rs._rs.compile(
        source, rs.machine_json(machine), cache_dir="", fetch=True, mutant=mutant
    )


def transfers(text):
    """Resolve each pack's source L1 address, independently of register naming."""
    registers, packed = {}, []
    for line in text.splitlines():
        load = re.fullmatch(r"\s*vunpk v(\d+) <- l1\[(\d+)\] f16", line)
        store = re.fullmatch(r"\s*vpack v(\d+) -> l1\[(\d+)\] (mx7[ab])", line)
        if load:
            registers[int(load[1])] = int(load[2])
        if store:
            packed.append((registers[int(store[1])], int(store[2]), store[3]))
    return packed


def test_finite_batch_padded_graph_keys(monkeypatch):
    machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    keys = []
    for batch, overlap, pipeline, zero in (
        (1, 0, 0, 0),
        (4, 0, 0, 0),
        (4, 1, 0, 0),
        (4, 0, 1, 0),
        (4, 0, 1, 1),
    ):
        monkeypatch.setenv("KTPU_QUANT_PIPELINE", str(pipeline))
        monkeypatch.setenv("KTPU_QUANT_ZERO_CLASSIFIER", str(zero))
        monkeypatch.setenv("KTPU_QUANT_CLASSIFIER_OVERLAP", str(overlap))
        monkeypatch.setenv("KTPU_PIPE_FINITE_BATCH", str(batch))
        monkeypatch.setenv("KTPU_PIPE_OVERLAP", "1")
        kernel, given = matmul.quant_kernel(
            Operand("full", pitch=48, offset=16),
            131,
            16,
            132,
            32,
            "b",
            33,
            1,
            finite=True,
        )
        source = json.dumps(kernel.trace(given, machine=K.dsl_machine(machine)).graph)
        result = rs._rs.compile(
            source, rs.machine_json(machine), cache_dir="", fetch=True
        )
        assert not result["verdict"]["refused"], result["verdict"]
        assert "vsync pack on=drain slack=1" in result["l1"]
        keys.append(json.loads(rs._rs.library_key(source, rs.machine_json(machine))))
    assert len({key["class"] for key in keys}) == 5
    assert all(key["versions"] == keys[0]["versions"] for key in keys)


@pytest.mark.parametrize("batch", [1, 4])
def test_zero_classifier_replaces_predicates(batch):
    baseline = compile_batch(batch, checked=True, pipeline=True)
    result = compile_batch(batch, checked=True, pipeline=True, zero_classifier=True)
    assert not result["verdict"]["refused"], result["verdict"]

    def witness(compiled):
        text = compiled["l1"]
        assert "vcmplt " not in text
        assert "vabs " not in text
        assert " if=p" not in text
        assert text.count("vmul ") == text.count("vexp2d ")
        assert text.count("vpack ") == baseline["l1"].count("vpack ")
        assert "vsync fe on=math\n    getstk s14" in text

    witness(result)
    undone = compile_batch(
        batch,
        checked=True,
        pipeline=True,
        zero_classifier=True,
        mutant="l2.packing/compare-classifier",
    )
    assert undone["l1"] == baseline["l1"]
    with pytest.raises(AssertionError):
        witness(undone)


def test_pipeline_uses_engine_fences_and_scoreboards():
    serial = compile_batch(4, checked=True)
    pipelined = compile_batch(4, checked=True, pipeline=True)
    assert not pipelined["verdict"]["refused"], pipelined["verdict"]

    def witness(result):
        text = result["l1"]
        assert text.count("vsync drain on=pack") == 5
        assert text.count("vsync fe on=pack") == 1  # final status image only
        assert re.search(r"vcmplt p\d+,.*\n +vsync fe on=math\n +vmov", text)
        assert text.count("vsync fe on=unpack") == 5
        assert text.count("vpack ") == serial["l1"].count("vpack ")

    witness(pipelined)
    undone = compile_batch(
        4, checked=True, pipeline=True, mutant="l2.packing/serial-engines"
    )
    assert undone["l1"] == serial["l1"]
    with pytest.raises(AssertionError):
        witness(undone)


def test_classifier_overlap_keeps_predicate_fences():
    serial = compile_batch(4, checked=True)
    overlapped = compile_batch(4, checked=True, classifier_overlap=True)
    assert not overlapped["verdict"]["refused"], overlapped["verdict"]

    def operations(text):
        return [s for s in text.splitlines() if "vsync fe on=math" not in s]

    assert operations(serial["l1"]) == operations(overlapped["l1"])

    def witness(result):
        text = result["l1"]
        assert text.count("vsync fe on=math") == 24
        assert re.search(r"vcmplt p\d+,.*\n +vsync fe on=math\n +vmov", text)
        assert re.search(r"vexp2d .*\n +vsync fe on=math\n +vpack", text)

    witness(overlapped)
    undone = compile_batch(
        4, checked=True, classifier_overlap=True, mutant="l2.packing/serial-classifier"
    )
    assert undone["l1"] == serial["l1"]
    with pytest.raises(AssertionError):
        witness(undone)


def test_finite_batch_barriers_and_byte_map():
    serial = compile_batch(1)
    batched = compile_batch(4)
    middle = compile_batch(2)
    for result in (serial, middle, batched):
        assert not result["verdict"]["refused"], result["verdict"]
        assert transfers(result["l1"]) == [
            (i * 8, 128 + i * 4, "mx7a") for i in range(16)
        ]
        assert "vsync pack on=drain slack=0" in result["l1"]
    assert serial["l1"].count("vsync fe on=pack") == 16
    assert middle["l1"].count("vsync fe on=pack") == 8

    def witness(result):
        assert result["l1"].count("vsync fe on=unpack") == 4
        assert result["l1"].count("vsync fe on=pack") == 4
        assert result["l1"].count("vpack ") == 16

    witness(batched)
    undone = compile_batch(4, mutant="l2.packing/no-finite-batch")
    assert undone["l1"] == serial["l1"]
    with pytest.raises(AssertionError):
        witness(undone)
    # The checked classifier remains byte-for-byte unchanged by this transform.
    checked = compile_batch(4, checked=True)
    assert (
        checked["l1"]
        == compile_batch(4, checked=True, mutant="l2.packing/no-finite-batch")["l1"]
    )
