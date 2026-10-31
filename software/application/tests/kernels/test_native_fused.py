"""Native fusion witnesses: graph edges, Rust packages, and library relocation."""

import json

import numpy as np
import pytest

from kohakutpu.application import api as kp
from kohakutpu.application.api import native
from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels.native import attention as A
from kohakutpu.application.kernels.native import linear as L
from kohakutpu.application.kernels.native import rows as R
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.class_library import ClassLibrary
from kohakutpu.application.runtime.device import Device
from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.application.runtime.template_store import TemplateStore
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def trace_linear(dev, rows, class_capacity=True):
    kernel = L.kernel(
        rows,
        64,
        32,
        2,
        2,
        "relu",
        True,
        8,
        class_capacity=class_capacity,
        machine_json=rs.machine_json(dev.machine),
    )
    return kernel.trace(
        {"x": (rows, 64), "w": (32, 64), "bias": (32,), "residual": (rows, 32)},
        machine=K.dsl_machine(dev.machine),
    )


def test_epilogue_activation_precedes_residual():
    values = {
        0: np.array([-4, 2], np.float32),
        1: np.array([1, -1], np.float32),
        2: np.array([5, -3], np.float32),
    }
    expected = np.maximum(values[0] + values[1], 0) + values[2]
    actual = X.evaluate(L.epilogue("relu", True), values)
    np.testing.assert_array_equal(actual, expected)
    mutant = X.op("max", X.op("add", X.op("add", X.Load(0), X.Load(1)), X.Load(2)), 0)
    with pytest.raises(AssertionError):
        np.testing.assert_array_equal(X.evaluate(mutant, values), expected)


def test_native_linear_class_two_shapes_two_addresses(tmp_path, monkeypatch):
    dev = Device(
        "offline",
        board="multimesh_v9t6",
        library=tmp_path,
        library_only=True,
        classes=True,
    )
    try:
        machine = rs.machine_json(dev.machine)
        traces = [trace_linear(dev, rows) for rows in (8, 16)]
        outputs = [
            rs._rs.compile(t.graph_json, machine, cache_dir="", fetch=True)
            for t in traces
        ]
        for output in outputs:
            assert output["bound"] is None
            assert not output["verdict"]["refused"], output["verdict"]
        raw = [o["packages"][0]["bytes"] for o in outputs]
        derived = rs._rs.template_derive(
            raw,
            json.dumps([{"member": 0}, {"member": 1}]),
            json.dumps({"member": {"lo": 0, "hi": 1}}),
            machine,
        )
        mutant = [
            rs._rs.compile(
                trace_linear(dev, rows, False).graph_json,
                machine,
                cache_dir="",
                fetch=True,
            )["packages"][0]["bytes"]
            for rows in (8, 16)
        ]
        with pytest.raises(ValueError, match="structure"):
            rs._rs.template_derive(
                mutant,
                json.dumps([{"member": 0}, {"member": 1}]),
                json.dumps({"member": {"lo": 0, "hi": 1}}),
                machine,
            )
        table = derived["template_json"]
        buffers = rs._rs.package_read(derived["bytes"], machine)["buffers"]
        bindings = [0x10000000 + i * 0x1000000 for i in range(len(buffers))]
        TemplateStore(tmp_path, machine).admit(
            traces[0].graph_json, derived["bytes"], table, bindings
        )
        library = ClassLibrary(tmp_path, machine)
        for index, (trace, output) in enumerate(zip(traces, outputs)):
            ref = {
                0: {
                    "source": traces[0].graph_json,
                    "table": table,
                    "params": {"member": index},
                }
            }
            library.admit(trace.graph_json, output, ref, {0: bindings})

        def forbidden(*args, **kwargs):
            raise AssertionError("runtime compiler or exact library lookup reached")

        monkeypatch.setattr(rs._rs, "compile", forbidden)
        monkeypatch.setattr(rs._rs, "library_get", forbidden)
        monkeypatch.setattr(rs._rs, "library_prebuild", forbidden)
        entries = [
            p for p in tmp_path.glob("*/classes/*.json") if p.name != "index.json"
        ]
        assert len(entries) == 1
        for trace, direct in zip(traces, raw):
            output = library.lookup(trace.graph_json)
            assert output["packages"][0]["bytes"] == derived["bytes"]
            for shift in (0, 0x100000):
                relocated = [b + shift for b in bindings]
                package = output["packages"][0]
                assert rs._rs.bind(
                    package["bytes"],
                    relocated,
                    machine,
                    0,
                    False,
                    params=package["template_params"],
                    template_json=package["template_json"],
                ) == rs._rs.bind(direct, relocated, machine, 0, False)
            compiled = dev.kernel(trace.graph_json)
            addresses = {
                name: dev.alloc(spec.nbytes()) for name, spec in compiled.params
            }
            dev.run(compiled, addresses)
        assert dev.counters["compile_calls"] == 0
        assert dev.counters["class_hits"] == 2
        assert dev.counters["exact_hits"] == 0
    finally:
        dev.close()


def test_native_linear_exact_library_two_addresses(tmp_path, monkeypatch):
    dev = Device("offline", board="multimesh_v9t6", library=tmp_path, library_only=True)
    try:
        traced = trace_linear(dev, 8)
        rs._rs.library_prebuild(
            str(tmp_path), traced.graph_json, rs.machine_json(dev.machine)
        )

        def forbidden(*args, **kwargs):
            raise AssertionError("runtime compiler reached")

        monkeypatch.setattr(rs._rs, "compile", forbidden)
        monkeypatch.setattr(rs._rs, "library_prebuild", forbidden)
        programs = []
        for shift in (0, 0x100000):
            compiled = dev.kernel(traced.graph_json)
            binds = {name: dev.alloc(spec.nbytes()) for name, spec in compiled.params}
            package = compiled.packages[0]
            addresses = [
                0x10000000 + shift + i * 0x1000000
                for i in range(len(package["params"]))
            ]
            programs.append(
                rs._rs.bind(
                    package["bytes"], addresses, rs.machine_json(dev.machine), 0, False
                )
            )
            dev.run(compiled, binds)
        assert programs[0] != programs[1]
        assert dev.counters["compile_calls"] == 0
        assert dev.counters["exact_hits"] == 2
        assert dev.counters["runs"] == 2
    finally:
        dev.close()


def test_native_api_knob_both_ways():
    counts, warm = {}, {}
    for fused in (False, True):
        with D.attached(Device("offline", board="multimesh_v9t6")) as dev:
            x = kp.tensor(np.ones((8, 64), np.float16))
            w1 = kp.tensor(np.ones((64, 64), np.float16))
            b1 = kp.tensor(np.zeros(64, np.float16))
            w2 = kp.tensor(np.ones((32, 64), np.float16))
            b2 = kp.tensor(np.zeros(32, np.float16))
            result = native.mlp(x, w1, b1, w2, b2, activation="relu", fused=fused)
            assert result.shape == (8, 32)
            counts[fused] = len(dev.runs)
            x2 = kp.tensor(np.full((8, 64), 0.5, np.float16))
            native.mlp(x2, w1, b1, w2, b2, activation="relu", fused=fused)
            warm[fused] = len(dev.runs) - counts[fused]
    print("native MLP cold launch counts", counts, "warm weights / new input", warm)
    assert counts[True] < counts[False]
    assert warm[True] == 1
    assert warm[False] > warm[True]


def test_native_rows_and_attention_packages():
    rng = np.random.default_rng(19)
    x = rng.standard_normal((4, 192)).astype(np.float32)
    scale, shift = rng.standard_normal((2, 192)).astype(np.float32)
    centered = x - x.mean(-1, keepdims=True)
    norm = centered / np.sqrt((centered * centered).mean(-1, keepdims=True) + 1e-5)
    np.testing.assert_allclose(
        X.evaluate(R.expression("layernorm", 192), {0: x, 1: scale, 2: shift}),
        norm * scale + shift,
        atol=2e-6,
    )
    p = np.exp(x - x.max(-1, keepdims=True))
    np.testing.assert_allclose(
        X.evaluate(R.expression("softmax", 192), {0: x}),
        p / p.sum(-1, keepdims=True),
        atol=1e-7,
    )
    machine = OfflineCard("multimesh_v9t6").machine
    cases = [
        (R.kernel("softmax", 8, 192, 2), {"in0": (8, 192)}),
        (
            R.kernel("layernorm", 8, 192, 2),
            {"in0": (8, 192), "in1": (192,), "in2": (192,)},
        ),
        (
            R.linear_kernel(
                8, 192, 64, 2, 2, machine_json=rs.machine_json("multimesh_v9t6")
            ),
            {
                "x": (8, 192),
                "scale": (192,),
                "shift": (192,),
                "w": (64, 192),
                "bias": (64,),
            },
        ),
        (A.kernel(1, 16, 32, 64, 2), {"q": (16, 64), "k": (32, 64), "vt": (64, 32)}),
    ]
    for kernel, given in cases:
        trace = kernel.trace(given, machine=K.dsl_machine(machine))
        output = rs._rs.compile(
            trace.graph_json, rs.machine_json(machine), cache_dir="", fetch=True
        )
        assert not output["verdict"]["refused"], output["verdict"]
        assert len(output["packages"]) == 1
        print(kernel.name, len(output["packages"][0]["bytes"]), output.get("scratch"))


def test_native_attention_mask_is_fp16_one(monkeypatch):
    machine = K.dsl_machine(OfflineCard("multimesh_v9t6").machine)

    def witness():
        trace = A.kernel(1, 16, 32, 64, 2).trace(
            {"q": (16, 64), "k": (32, 64), "vt": (64, 32)}, machine=machine
        )
        nonzero = [
            dict(node["kw"])
            for node in trace.graph["body"]
            if node.get("op") == "fill" and dict(node["kw"])["value"]["int"] != 0
        ]
        for attrs in nonzero:
            assert attrs["ew"]["int"] == 16
            bits = np.array([attrs["value"]["int"]], np.uint16)
            np.testing.assert_array_equal(bits.view(np.float16), np.ones(1, np.float16))
        assert len(nonzero) == 1

    witness()
    original = A.kt.fill_value

    def wrong(dst, value, **kwargs):
        if value:
            value = 1
        return original(dst, value, **kwargs)

    monkeypatch.setattr(A.kt, "fill_value", wrong)
    with pytest.raises(AssertionError):
        witness()


def test_native_mlp_has_two_internal_activation_quants():
    dev = Device("offline", board="multimesh_v9t6")
    try:
        kernel = L.mlp_kernel(
            8, 64, 64, 32, 2, 2, machine_json=rs.machine_json("multimesh_v9t6")
        )
        trace = kernel.trace(
            {"x": (8, 64), "w1": (64, 64), "b1": (64,), "w2": (32, 64), "b2": (32,)},
            machine=K.dsl_machine(dev.machine),
        )
        quants = [s for s in trace.graph["body"] if s.get("op") == "quant"]
        assert len(quants) == 2
        assert all(
            p["spec"]["dtype"] == "f16"
            for p in trace.graph["ports"]
            if p["name"] in ("x", "out")
        )
        output = rs._rs.compile(
            trace.graph_json, rs.machine_json(dev.machine), cache_dir="", fetch=True
        )
        assert not output["verdict"]["refused"], output["verdict"]
    finally:
        dev.close()
