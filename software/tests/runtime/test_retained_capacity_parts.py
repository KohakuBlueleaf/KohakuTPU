"""Runtime-stage outcome witness: real composition, writer, checker and replay.

The advertised resident arena is small but valid: an ostpu boot may reserve any
admitted even task capacity. Real launches exceed it while each launch fits.
On base this test reaches ResidentGraph.encode on the unsplit artifact and
fails its byte-capacity check, rather than failing an import of a new API.
"""

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from kohakutpu.application.runtime import generated
from kohakutpu.application.runtime import packages as B
from kohakutpu.application.runtime.device import Device
from kohakutpu.application.runtime.graph.native import NativeLaunch
from kohakutpu.application.runtime.graph.split import SplitRunner
from kohakutpu.application.runtime.os import abi as A
from kohakutpu.application.runtime.os.graph import Graph, ResidentGraph, task_capacity
from kohakutpu.application.runtime.split import BoundGraphArtifact
from kohakutpu.language.text import module as M
from kohakutpu.stack import rs


class AdmissionResident:
    """The real resident encoder, without transport or a running OS."""

    data_ref = 0
    tasks_pa = 0
    data_pa = 0
    capabilities = A.OSTPU_CAP_SLOTS | A.OSTPU_CAP_EPOCH | A.OSTPU_CAP_LOCAL
    encode = ResidentGraph.encode

    def __init__(self, capacity):
        self.capacity = capacity

    def _active(self, owner=None):
        pass


def _units(dev):
    return {
        (mesh.index, *coord): index
        for mesh in dev.machine.meshes
        for index, coord in enumerate(
            coord for coords in mesh.units.values() for coord in coords
        )
    }


def _packets(dev, address, count):
    operand = dict(kind="full", dtype="f16", offset=0, pitch=32, rows=4)
    result = []
    for index in range(count):
        graph = generated.template_graph(
            "vector.rows_kernel",
            dict(
                e=dict(
                    nodes=[
                        {"load": 0},
                        {"const": 2.0},
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
        )
        # In-place recurrence makes omission, duplication and order observable.
        result.append(NativeLaunch(graph, {"in0": address, "out": address}, "dsl"))
    return result


def test_over_capacity_composition_splits_and_executes_every_launch_once_in_order():
    dev = Device(kind="double", board="multimesh_v9t6", nodes=1)
    owned = []
    try:
        address = dev.alloc(256)
        packets = _packets(dev, address, 8)
        dependencies = [()] + [(i - 1,) for i in range(1, len(packets))]
        units = _units(dev)
        wait_window = json.loads(dev.card.machine_json)["machine"]["flaws"]["F-004"][
            "present"
        ]

        def prepare(parts, deps):
            item = SplitRunner(dev).prepare(parts, deps)
            owned.append(item)
            return item

        largest = 0
        for packet in packets:
            single = prepare([packet], [()])
            graph = Graph.package_parts(
                single.bind_artifact(), units, wait_window=wait_window
            )[0]
            slots = (len(graph.pack()) + A.OSTPU_TASK_BYTES - 1) // A.OSTPU_TASK_BYTES
            largest = max(largest, task_capacity(slots))
        original = prepare(packets, dependencies)
        original_artifact = original.bind_artifact()
        graph = Graph.package_parts(original_artifact, units, wait_window=wait_window)[
            0
        ]
        assert 0 < largest < A.KDS_MAXTASKS
        assert len(graph.tasks) <= largest
        resident = AdmissionResident(largest)
        with pytest.raises(ValueError, match="byte capacity"):
            resident.encode(graph)

        # Fitting composition remains the original bytes. On base this removes
        # no comments; on the overlay it checks the new provenance is inert.
        plain = replace(
            original.compiled,
            _progs={},
            l1=M.Text(
                "".join(
                    line
                    for line in original.compiled.l1.source.splitlines(keepends=True)
                    if not line.startswith("        # retained-cut ")
                )
            ),
        )
        assert original.bound_bindings is not None
        physical = {
            name: dev.mmu.resolve(at)[1] for name, at in original.bound_bindings.items()
        }
        with rs.compiler_owner(dev):
            plain_programs = B.placed(
                plain.programs(physical, dev.home), dev.card.nodes
            )
        assert {n: p.bound(True) for n, p in plain_programs.items()} == {
            part.node: part.package for part in original_artifact.parts
        }

        dev.card.sessions = {0: resident}
        dev.card.units, dev.card.unit_types = units, None
        dev.card.wait_window = wait_window
        dev.graph_backend = "kaos"
        retained = prepare(packets, dependencies)
        artifact = retained.bind_artifact()
        stages = getattr(artifact, "stages", (artifact,))
        runs = []
        for stage in stages:
            graphs = Graph.package_parts(stage, units, wait_window=wait_window)
            for part in stage.parts:
                # Construction passed the real writer; receiving-board admission
                # and the actual resident encoder must also accept every part.
                B.admit(part.package, dev.card.machine_json, part.node)
                resident.encode(graphs[part.node])  # base fails here on the whole graph
            runs.append({part.node: part.package for part in stage.parts})
        assert not isinstance(artifact, BoundGraphArtifact)
        assert [
            i for first, end in artifact.intervals for i in range(first, end)
        ] == list(range(8))
        verdict = json.loads(
            rs._rs.package_check_memory(
                runs + runs,
                dev.card.machine_json,
                {},
                compositional=True,
                independent_packages=True,
                host_join_every=len(runs),
            )
        )
        assert not verdict["refused"], verdict

        # Execute the actual L1 part programs, not the old graph or a hand-written
        # evaluator. All recurrence values are exactly representable in FP16.
        dev.graph_backend = None
        for seed in (0, 1):
            dev.write(address, np.full(128, seed, dtype=np.float16).tobytes())
            dev.collect_through(retained.submit())
            expected = seed
            for increment in range(1, 9):
                expected = 2 * expected + increment
            assert np.all(
                np.frombuffer(dev.read(address, 256), dtype=np.float16) == expected
            )
            assert retained.bind_artifact() is artifact

        # New physical inputs must bind every capacity part, not only part zero.
        other = dev.alloc(256)
        dev.write(other, np.full(128, 2, dtype=np.float16).tobytes())
        before = dev.read(address, 256)
        dev.graph_backend = "kaos"
        rebound = prepare(_packets(dev, other, 8), dependencies)
        rebound_artifact = rebound.bind_artifact()
        assert not isinstance(rebound_artifact, BoundGraphArtifact)
        for stage in rebound_artifact.stages:
            resident.encode(
                Graph.package_parts(stage, units, wait_window=wait_window)[0]
            )
        dev.graph_backend = None
        dev.collect_through(rebound.submit())
        expected = 2
        for increment in range(1, 9):
            expected = 2 * expected + increment
        assert np.all(np.frombuffer(dev.read(other, 256), dtype=np.float16) == expected)
        assert dev.read(address, 256) == before
        # A new proof epoch invalidates all part/adaptor caches, even though a
        # normal TinyJit input copy keeps its pinned addresses unchanged.
        old_parts = rebound.part_compiled
        dev.table_epoch += 1
        dev.graph_backend = "kaos"
        rebuilt = rebound.bind_artifact()
        assert rebuilt is not rebound_artifact
        assert all(
            new is not old
            for new, old in zip(rebound.part_compiled, old_parts, strict=True)
        )
        dev.graph_backend = None
        dev.write(other, np.zeros(128, dtype=np.float16).tobytes())
        dev.collect_through(rebound.submit())
        expected = 0
        for increment in range(1, 9):
            expected = 2 * expected + increment
        assert np.all(np.frombuffer(dev.read(other, 256), dtype=np.float16) == expected)
        _independent_list_boundaries()
    finally:
        dev.graph_backend = None
        for item in reversed(owned):
            item.close()
        dev.close()


def _independent_list_boundaries():
    """A missing release must race across a package cut, not be host-fenced.

    Called only after the capacity/numeric witness (base fails before this new
    proof API). The last read also tests the real host join between epochs.
    """
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

    def ring(receiver):
        return ["ring", receiver, {"slot": 9}]

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
        runs = [first, last, first, last]
        host_fenced = json.loads(rs._rs.package_check_memory(runs, machine, {}))
        assert not host_fenced["refused"], host_fenced
        checked = json.loads(
            rs._rs.package_check_memory(
                runs,
                machine,
                {},
                independent_packages=True,
                host_join_every=2,
            )
        )
        assert checked["refused"] == missing, checked
        if missing:
            assert any(f["kind"] == "race" for f in checked["findings"]), checked
        else:
            unjoined_epochs = json.loads(
                rs._rs.package_check_memory(
                    runs,
                    machine,
                    {},
                    independent_packages=True,
                )
            )
            assert unjoined_epochs["refused"], unjoined_epochs
