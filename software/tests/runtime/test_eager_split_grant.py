"""Eager row programs use the same grant and cache as contractions."""

import numpy as np

from kohakutpu.application.api import native
from kohakutpu.application.api.tensor import tensor
from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels.native import rows as native_rows
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import device as card
from kohakutpu.application.runtime import generated
from kohakutpu.application.runtime.call import call


def test_eager_rows_select_the_device_grant_and_reuse_it(monkeypatch):
    values = (np.arange(16 * 32) % 97 - 48).astype(np.float16)
    arguments = generated.rows_arguments(
        X.op("add", X.Load(0), 1.0),
        (Operand("full", pitch=32, rows=16),),
        16,
        32,
        Operand("full", pitch=32, rows=16),
    )
    outputs = []
    for count in (1, 4):
        dev = card.Device(kind="double", board="multimesh_v9t8", nodes=count)
        try:
            src, dst = dev.alloc(values.nbytes), dev.alloc(values.nbytes)
            dev.write(src, values.tobytes())
            first = generated.launch(
                dev, "vector.rows_kernel", arguments, {"in0": src, "out": dst}
            )
            assert first.info["active_nodes"] == count
            assert first.info["option_id"]
            compiled = first.info["compiled"]
            if count == 1:
                ordinary = generated.graph(
                    *generated._call(dev, "vector.rows_kernel", arguments)
                )
                assert dev.kernel(ordinary) is compiled
            with monkeypatch.context() as patch:
                patch.setattr(
                    generated.rs._rs,
                    "generate_split",
                    lambda *args: (_ for _ in ()).throw(AssertionError("cache miss")),
                )
                second = generated.launch(
                    dev, "vector.rows_kernel", arguments, {"in0": src, "out": dst}
                )
            assert second.info["compiled"] is compiled
            outputs.append(dev.read(dst, values.nbytes))
        finally:
            dev.close()
    assert outputs[1] == outputs[0] == (values + np.float16(1)).tobytes()


def test_native_convolution_records_its_single_and_multi_node_plan():
    rng = np.random.default_rng(19)
    arrays = [
        (rng.standard_normal(shape) / 8).astype(np.float16)
        for shape in ((1, 4, 4, 32), (128, 32), (128,))
    ]
    outputs = []
    for count in (1, 4):
        dev = card.Device(kind="double", board="multimesh_v9t8", nodes=count)
        try:
            with card.attached(dev):
                x, weight, bias = (tensor(value) for value in arrays)
                outputs.append(native.conv2d(x, weight, bias).numpy().tobytes())
                (run,) = [run for run in dev.runs if run.kind == "custom"]
                assert run.info["nodes"] == tuple(dev.card.nodes)
                assert run.info["option_id"]
                assert run.info["active_nodes"] == count
        finally:
            dev.close()
    assert outputs[1] == outputs[0]


def test_standalone_template_specialisation_keeps_grants_separate(monkeypatch):
    dev = card.Device(kind="double", board="multimesh_v9t8", nodes=4)
    values = np.random.default_rng(61).standard_normal((16, 32)).astype(np.float16)
    kernel = native_rows.kernel("softmax", 16, 32, dev.machine.count("VC"))
    try:
        with dev.on_node(0):
            first, one = call(dev, kernel, {"in0": values})
        second, four = call(dev, kernel, {"in0": values})
        assert one.info["p"] == 1
        assert four.info["p"] == four.info["active_nodes"] == 4
        assert one.info["nodes"] == (0,)
        assert four.info["nodes"] == tuple(dev.card.nodes)
        assert first["out"].tobytes() == second["out"].tobytes()
        with monkeypatch.context() as patch:
            patch.setattr(
                generated.rs._rs,
                "generate_split",
                lambda *args: (_ for _ in ()).throw(AssertionError("cache miss")),
            )
            with dev.on_node(0):
                repeated, again = call(dev, kernel, {"in0": values})
        assert again.info["compiled"] is one.info["compiled"]
        assert again.info["p"] == 1
        assert repeated["out"].tobytes() == first["out"].tobytes()
    finally:
        dev.close()
