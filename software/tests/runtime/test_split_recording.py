"""Automatic split-recording preparation preserves host-effect boundaries."""

import numpy as np

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import device as card
from kohakutpu.application.runtime import generated


def test_recorded_split_regions_replay_once_per_host_boundary(monkeypatch):
    monkeypatch.setenv("KTPU_GRAPH_FUSE", "1")
    dev = card.Device(kind="double", board="multimesh_v9t8", nodes=4)
    log = None
    values = (np.arange(16 * 32) % 41).astype(np.float16)
    operand = Operand("full", pitch=32, rows=16)
    arguments = generated.rows_arguments(
        X.op("add", X.Load(0), 1.0), (operand,), 16, 32, operand
    )
    try:
        x, y, z = (dev.alloc(values.nbytes) for _ in range(3))
        dev.write(x, values.tobytes())
        dev.record()
        for iteration in range(2):
            if iteration:
                dev.write(x, (values * np.float16(2)).tobytes())
            for src, dst in ((x, y), (y, z)):
                generated.launch(
                    dev, "vector.rows_kernel", arguments, {"in0": src, "out": dst}
                )
        log = dev.recorded()
        dev.free(y)
        dev.prepare_recorded(log)
        dev.free(x)
        assert log.split is not None
        regions = [body for kind, body in log.split.segments if kind == "run"]
        assert all(region.dependencies == ((), (0,)) for region in regions)
        published = []
        submit = dev.card.submit_launch

        def publish(*args):
            result = submit(*args)
            published.append(result)
            return result

        monkeypatch.setattr(dev.card, "submit_launch", publish)
        for _ in range(2):
            dev.write(x, values.tobytes())
            dev.write(y, bytes([0xFF]) * values.nbytes)
            dev.write(z, bytes([0xFF]) * values.nbytes)
            runs = dev.replay(log)
            assert len(runs) == 2
            assert (
                dev.read(z, values.nbytes)
                == (values * np.float16(2) + np.float16(2)).tobytes()
            )
        assert len(published) == 4
        for pending in published:
            assert pending.order == [
                *(("publish", node) for node in dev.card.nodes),
                *(("collect", node) for node in dev.card.nodes),
            ]
    finally:
        if log is not None:
            log.close()
        dev.close()


def test_split_recording_fuses_private_edges_before_slot_lowering(monkeypatch):
    monkeypatch.setenv("KTPU_GRAPH_FUSE", "1")
    dev = card.Device(kind="double", board="multimesh_v9t8", nodes=4)
    log = None
    values = (np.arange(16 * 32) % 37).astype(np.float16)
    operand = Operand("full", pitch=32, rows=16)
    try:
        x, y, z = (dev.alloc(values.nbytes) for _ in range(3))
        dev.write(x, values.tobytes())
        dev.record()
        for src, dst, op, scalar in ((x, y, "mul", 0.5), (y, z, "add", 1.0)):
            generated.launch(
                dev,
                "vector.rows_kernel",
                generated.rows_arguments(
                    X.op(op, X.Load(0), scalar), (operand,), 16, 32, operand
                ),
                {"in0": src, "out": dst},
            )
        log = dev.recorded()
        dev.free(y)
        dev.prepare_recorded(log)
        assert log.split.rules == ["elementwise"]
        assert (log.split.before, log.split.after) == (2, 1)
        ((kind, region),) = log.split.segments
        assert kind == "run"
        assert y not in region.bindings.values()
        for factor in (1, 2):
            current = values * np.float16(factor)
            dev.write(x, current.tobytes())
            dev.write(z, bytes([0xFF]) * values.nbytes)
            dev.replay(log)
            assert (
                dev.read(z, values.nbytes)
                == (current * np.float16(0.5) + np.float16(1)).tobytes()
            )
    finally:
        if log is not None:
            log.close()
        dev.close()
