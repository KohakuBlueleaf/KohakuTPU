"""Prepared consumer destination stride and measured cost admission witnesses."""

import json

import numpy as np

from application.tests.runtime.pipe_artifacts import (  # noqa: F401
    case,
    fold_record,
    pipe_library,
)
from application.tests.runtime.test_packing_model import reference
from application.tests.runtime.test_pipe_batch import model  # noqa: F401
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import costs
from kohakutpu.application.runtime.forms import Step
from kohakutpu.language.numerics import mxfp7
from kohakutpu.stack import kernel as K


def test_measured_cost_is_board_specific_and_layout_equivalent(tmp_path, monkeypatch):
    # These costs were recorded with the original schedule, not today's defaults.
    # Keep the measurement's provenance; never relabel stale costs as current.
    for name in (
        "KTPU_QUANT_CLASSIFIER_OVERLAP",
        "KTPU_QUANT_PIPELINE",
        "KTPU_QUANT_ZERO_CLASSIFIER",
        "KTPU_QUANT_STREAM_AXES",
        "KTPU_QUANT_FILL_OVERLAP",
        "KTPU_QUANT_ZERO_REUSE",
        "KTPU_PULL_TREE",
    ):
        monkeypatch.setenv(name, "0")
    monkeypatch.setenv("KTPU_CLASS_BAND", "1")
    monkeypatch.setenv("KTPU_PIPE_FINITE_BATCH", "4")
    monkeypatch.setenv("KTPU_PIPE_OVERLAP", "1")
    step = Step(1000, 840, ((84, 1), (10, 84)), 0, ((84, 32), (10, 1)), 0, 2688)
    quant = costs.quant_recipe(Operand("full", pitch=32), 84, 16, 96, 32, "b", 8, 1, 1)
    recipe = dict(quant=quant, steps=costs.steps_recipe((step,)))
    records = {
        costs.key("prepared_quant", dict(board="test", recipe=recipe)): dict(
            original=200, alternative=150, verified=True
        )
    }
    prefix = Step(1000, 840, ((840, 1),), 0, ((840, 1),), 0, 840)
    compound = dict(quant=quant, steps=costs.steps_recipe((prefix, step)))
    records[costs.key("prepared_quant", dict(board="test", recipe=compound))] = dict(
        original=300, alternative=180, verified=True
    )
    padding = costs.quant_recipe(Operand("full", pitch=32), 6, 32, 32, 32, "b", 8, 1, 1)
    records[costs.key("staged_padding", dict(board="test", recipe=padding))] = dict(
        original=200, alternative=150, verified=True
    )
    records[costs.key("staged_padding", dict(board="model", recipe=padding))] = dict(
        original=150, alternative=200, verified=True
    )
    records[costs.key("staged_padding", dict(board="unverified", recipe=padding))] = (
        dict(original=200, alternative=100, verified=False)
    )
    path = tmp_path / "costs.json"
    path.write_text(
        json.dumps(dict(schedule=dict(finite_batch=4, overlap=True), recipes=records))
    )
    monkeypatch.setenv("KTPU_COST_PROFILE", str(path))
    dense = Step(2000, 840, step.walk, 0, ((84, 10), (10, 1)), 0, 840)
    args = (Operand("full", pitch=10), 84, 10, 96, 32, "b", 8, 1, 1, "test")
    assert costs.prepared_quant((dense,), *args) == json.loads(json.dumps(recipe))
    wrong = Step(2000, 840, ((84, 10), (10, 1)), 0, dense.dwalk, 0, 840)
    assert costs.prepared_quant((wrong,), *args) is None
    assert costs.choose("staged_padding", padding, "test")
    assert not costs.choose("staged_padding", padding, "model")
    assert not costs.choose("staged_padding", padding, "unmeasured")
    assert not costs.choose("staged_padding", padding, "unverified")
    assert costs.has_steps((step,), "test")
    assert not costs.has_steps((dense,), "test")
    assert costs.has_steps((prefix, step), "test")
    assert costs.prepared_quant((prefix, dense), *args) == compound
    bad_prefix = Step(1000, 840, ((840, 0),), 0, ((840, 1),), 0, 840)
    assert costs.prepared_quant((bad_prefix, dense), *args) is None
    # Reverse provenance witness: the same recipe must be rejected under a
    # changed schedule, including every newly promoted default.
    for name, value, previous in (
        ("KTPU_QUANT_PIPELINE", "1", "0"),
        ("KTPU_QUANT_ZERO_CLASSIFIER", "1", "0"),
        ("KTPU_QUANT_STREAM_AXES", "1", "0"),
        ("KTPU_QUANT_FILL_OVERLAP", "1", "0"),
        ("KTPU_QUANT_ZERO_REUSE", "1", "0"),
        ("KTPU_PULL_TREE", "1", "0"),
        ("KTPU_CLASS_BAND", "4", "1"),
    ):
        monkeypatch.setenv(name, value)
        assert costs.prepared_quant((dense,), *args) is None
        assert not costs.choose("staged_padding", padding, "test")
        monkeypatch.setenv(name, previous)
    monkeypatch.setenv("KTPU_PIPE_FINITE_BATCH", "1")
    assert not costs.choose("staged_padding", padding, "test")


def test_prepared_quant_explicit_table_stride_bytes_and_mutant(
    model, fold_record  # noqa: F811
):
    dev = model
    rec = fold_record(2944)
    q = case("fold_2944")["c"]
    rng = np.random.default_rng(119)
    values = (rng.integers(-500, 501, (84, 120)) / 64).astype(np.float16)
    padded = np.zeros((q["rp"], q["cp"]), np.float16)
    padded[:84, :120] = values
    expected = reference(mxfp7.lift(padded, q["nk"]), q["side"], q["g"], q["nk"])
    buffers = {p: dev.alloc(n + 256) for p, n in rec["allocations"].items()}
    for label in ("before", "after", "mutant"):
        for p, n in rec["allocations"].items():
            raw = (
                bytes.fromhex(rec["tables"][p])
                if p in rec["tables"]
                else values.tobytes() if p == "p_x" else bytes(n)
            )
            dev.write(buffers[p], bytes([0xD3]) * 128 + raw + bytes([0xD3]) * 128)
        stages = rec["before"] if label == "before" else [rec["after"]]
        for stage in stages:
            l1 = stage["l1"]
            if label == "mutant":
                assert "pitch=64" in l1
                l1 = l1.replace("pitch=64", "pitch=32")
            c = K.compile(l1, name=stage["name"], machine=dev.machine, level="l1")
            # A too-small row stride may be rejected as sub-line ownership.
            if label == "mutant" and c.verdict["refused"]:
                assert any(f["clause"] == "L1.VER.017" for f in c.verdict["findings"])
                break
            assert not c.verdict["refused"]
            dev.run(c, {p: buffers[b] + 128 for p, b in stage["binds"].items()})
        else:
            got = dev.read(buffers["q"], rec["allocations"]["q"] + 256)
            assert got[:128] == got[-128:] == bytes([0xD3]) * 128
            assert (got[128:-128] == expected) == (label != "mutant")
    for addr in buffers.values():
        dev.free(addr)


def test_prepared_tree_retains_partial_rounding(model, fold_record):  # noqa: F811
    dev = model
    rec = fold_record(2922)
    rng = np.random.default_rng(42)
    buffers = {p: dev.alloc(n + 256) for p, n in rec["allocations"].items()}
    data = {
        p: (
            bytes.fromhex(rec["tables"][p])
            if p in rec["tables"]
            else rng.uniform(-2, 2, n // 2).astype(np.float16).tobytes()
        )
        for p, n in rec["allocations"].items()
    }
    after = rec["after"]
    assert "vmax " in after["l1"]
    mutant = dict(after, l1=after["l1"].replace("vmax ", "vmin "))
    outputs = []
    for stages in (rec["before"], [after], [mutant]):
        for p, raw in data.items():
            dev.write(buffers[p], bytes([0xD3]) * 128 + raw + bytes([0xD3]) * 128)
        for stage in stages:
            c = K.compile(
                stage["l1"], name=stage["name"], machine=dev.machine, level="l1"
            )
            assert not c.verdict["refused"]
            dev.run(c, {p: buffers[b] + 128 for p, b in stage["binds"].items()})
        out = dev.read(buffers["out"], rec["allocations"]["out"] + 256)
        assert out[:128] == out[-128:] == bytes([0xD3]) * 128
        outputs.append(out)
    assert outputs[0] == outputs[1] != outputs[2]
    for addr in buffers.values():
        dev.free(addr)
