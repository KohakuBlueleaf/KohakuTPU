"""Production compile/bind/proof work for 8 and 32 launches, not event counts alone.

The real resident encoder's byte arena is the capacity boundary. We first prove
that the unsplit composition cannot fit it. The measured path uses the supported
one-launch part knob and calls the real composer, lowering, writer, admission,
and retained-sequence proof. No compiler/proof result is stubbed.
"""

import json
import time
from pathlib import Path

import numpy as np
import pytest
from test_retained_capacity_parts import AdmissionResident, _units

from kohakutpu.application.runtime import generated
from kohakutpu.application.runtime.device import Device
from kohakutpu.application.runtime.graph.native import NativeLaunch
from kohakutpu.application.runtime.graph.parts import BoundGraphSequence
from kohakutpu.application.runtime.graph.split import SplitRunner
from kohakutpu.application.runtime.os import abi as A
from kohakutpu.application.runtime.os.graph import Graph, task_capacity
from kohakutpu.stack import rs


def packets(dev, address, count):
    operand = dict(kind="full", dtype="f16", offset=0, pitch=32, rows=4)
    return [
        NativeLaunch(
            generated.template_graph(
                "vector.rows_kernel",
                dict(
                    e=dict(
                        nodes=[
                            {"load": 0},
                            {"const": -1.0},
                            {"op": "mul", "args": [0, 1]},
                            {"const": float(index + 1)},
                            {"op": "add", "args": [2, 3]},
                        ],
                        root=4,
                    ),
                    operands=[operand],
                    R=4,
                    W=32,
                    out=operand,
                ),
                dev.machine,
            ),
            {"in0": address, "out": address},
            "dsl",
        )
        for index in range(count)
    ]


def test_real_capacity_parts_total_compile_and_proof_work(monkeypatch, record_property):
    # Both sizes rebuild with the same cold disk-cache policy. Timed scope
    # includes eager accounting, planning, composition, lowering, exact admission
    # and both proof epochs. process_time_ns includes native CPU time as well as
    # Python; event-only counters cannot hide history copying in this assertion.
    monkeypatch.setenv("KTPU_COMPILE_CACHE", "")
    original_compose = rs._rs.compile_split_graph
    calls = []

    def compose(*args, **kwargs):
        calls.append(len(kwargs["launches"]))
        return original_compose(*args, **kwargs)

    monkeypatch.setattr(rs._rs, "compile_split_graph", compose)
    measurements = []
    for count in (8, 32):
        dev = Device(kind="double", board="multimesh_v9t6", nodes=1)
        owned = []
        try:
            address = dev.alloc(256)
            launches = packets(dev, address, count)
            dependencies = [()] + [(i - 1,) for i in range(1, count)]
            units = _units(dev)
            wait_window = json.loads(dev.card.machine_json)["machine"]["flaws"][
                "F-004"
            ]["present"]

            # Use the actual maximum single-launch resident footprint, not an
            # artificially reduced package writer limit or fabricated usage.
            largest = 0
            for launch in launches:
                single = SplitRunner(dev).prepare([launch], [()])
                owned.append(single)
                graph = Graph.package_parts(
                    single.bind_artifact(), units, wait_window=wait_window
                )[0]
                slots = (
                    len(graph.pack()) + A.OSTPU_TASK_BYTES - 1
                ) // A.OSTPU_TASK_BYTES
                largest = max(largest, task_capacity(slots))
            resident = AdmissionResident(largest)
            whole = SplitRunner(dev).prepare(launches, dependencies)
            owned.append(whole)
            whole_graph = Graph.package_parts(
                whole.bind_artifact(), units, wait_window=wait_window
            )[0]
            with pytest.raises(ValueError, match="capacity"):
                resident.encode(whole_graph)

            dev.card.sessions = {0: resident}
            dev.card.units, dev.card.unit_types = units, None
            dev.card.wait_window = wait_window
            dev.graph_backend = "kaos"
            calls.clear()
            started = time.process_time_ns()
            prepared = SplitRunner(dev, max_part_launches=1).prepare(
                launches, dependencies
            )
            owned.append(prepared)
            artifact = prepared.bind_artifact()
            cpu_ns = time.process_time_ns() - started
            assert isinstance(artifact, BoundGraphSequence)
            assert calls == [1] * count
            assert artifact.intervals == tuple((i, i + 1) for i in range(count))
            assert prepared.proof_work is not None
            work = prepared.proof_work
            assert work["checked_parts"] == 2 * count
            assert work["history_visits"] > 0
            package_bytes = sum(
                len(part.package) for stage in artifact.stages for part in stage.parts
            )
            for stage in artifact.stages:
                resident.encode(
                    Graph.package_parts(stage, units, wait_window=wait_window)[0]
                )
            measurements.append(
                dict(launches=count, cpu_ns=cpu_ns, package_bytes=package_bytes, **work)
            )

            # Execute every compiled part at its own bindings. This bounded
            # alternating recurrence is order-sensitive and exactly FP16 even
            # for 32 launches (unlike an exponentially growing recurrence).
            dev.graph_backend = None
            for seed in (0, 3):
                dev.write(address, np.full(128, seed, dtype=np.float16).tobytes())
                dev.collect_through(prepared.submit())
                expected = seed
                for increment in range(1, count + 1):
                    expected = -expected + increment
                assert np.all(
                    np.frombuffer(dev.read(address, 256), dtype=np.float16) == expected
                )
                assert prepared.bind_artifact() is artifact
        finally:
            dev.graph_backend = None
            for item in reversed(owned):
                item.close()
            dev.close()
    small, large = measurements
    record_property("retained_total_work", json.dumps(measurements, sort_keys=True))
    assert large["package_bytes"] <= 5 * small["package_bytes"]
    assert large["history_visits"] <= 8 * small["history_visits"]
    # This checks total CPU, not just the verifier event count. A factor-eight
    # envelope allows logarithmic exact-history queries and host noise while
    # rejecting the factor-sixteen signature of quadratic growth at 4x input.
    assert large["cpu_ns"] <= 8 * small["cpu_ns"], measurements


def test_retained_proof_rejects_balanced_missing_release_without_whole_replay():
    machine = (
        Path(__file__).resolve().parents[3] / "machines/multimesh_v9t6.json"
    ).read_text()

    def build(node, calls):
        return rs._rs.package_build(
            json.dumps(dict(credit=32, mesh=node)),
            json.dumps([*calls, ["end"]]),
            machine,
            node,
        )

    def ring(node):
        return ["ring", node, {"slot": 9}]

    wait = ["wait_slot", 9, 1, {"consume": True}]
    for missing in (False, True):
        first = {
            0: build(
                0,
                [["store", [[0x4000, 7]]], ["barrier"], wait]
                + ([] if missing else [ring(1)]),
            ),
            1: build(1, [["barrier"], ring(0)] + ([] if missing else [wait])),
        }
        last = {
            0: build(0, [["barrier"]]),
            1: build(1, [["load", 0, 0x4000], ["barrier"]]),
        }
        verdict = json.loads(
            rs._rs.package_check_memory(
                [first, last, first, last],
                machine,
                {},
                compositional=True,
                independent_packages=True,
                host_join_every=2,
                retained_parts=True,
            )
        )
        assert verdict["refused"] is missing, verdict
        assert verdict["part_proof"]["checked_parts"] == (0 if missing else 4)
