"""Real composition/binding must stay additive through the KAOS translator.

Selected by tests/run.py's runtime stage. This is an offline translation
witness, not a claim of KAOS execution or numerical correctness on a card.
"""

import itertools
import json

import pytest

from kohakutpu.application.runtime import generated
from kohakutpu.application.runtime.device import Device
from kohakutpu.application.runtime.graph.native import NativeLaunch
from kohakutpu.application.runtime.graph.split import SplitRunner
from kohakutpu.application.runtime.os import abi as A
from kohakutpu.application.runtime.os.graph import Graph
from kohakutpu.stack import rs


@pytest.mark.parametrize("board", ["multimesh_v9t6", "multimesh_v9t8"])
@pytest.mark.parametrize("nodes", [2, 4])
def test_retained_kaos_translation_costs_its_launches_plus_joins(board, nodes):
    dev = Device(kind="offline", board=board, nodes=nodes)
    prepared = []
    try:
        rows, width, launches = 64, 32, 8
        operand = dict(kind="full", dtype="f16", offset=0, pitch=width, rows=rows)
        arguments = dict(
            e=dict(
                nodes=[{"load": 0}, {"const": 1.0}, {"op": "add", "args": [0, 1]}],
                root=2,
            ),
            operands=[operand],
            R=rows,
            W=width,
            out=operand,
        )
        source, _ = generated.split_graph(
            "vector.rows_kernel",
            json.dumps(arguments),
            rs.machine_json(dev.machine),
            None,
            tuple(dev.card.nodes),
            "rows",  # Exercise the sharded candidate, not an opportunistic p1 plan.
        )
        graph = json.loads(source)
        addresses = [dev.alloc(rows * width * 2) for _ in range(launches + 1)]
        packets = [
            NativeLaunch(graph, {"in0": a, "out": b}, "dsl")
            for a, b in itertools.pairwise(addresses)
        ]
        units = {
            (mesh.index, *coord): index
            for mesh in dev.machine.meshes
            for index, coord in enumerate(
                coord for coords in mesh.units.values() for coord in coords
            )
        }
        wait_window = json.loads(dev.card.machine_json)["machine"]["flaws"]["F-004"][
            "present"
        ]

        def lower(packets, dependencies):
            retained = SplitRunner(dev).prepare(packets, dependencies)
            prepared.append(retained)
            return Graph.package_parts(
                retained.bind_artifact(), units, wait_window=wait_window
            )

        launch_bytes = dict.fromkeys(dev.card.nodes, 0)
        for packet in packets:
            for node, part in lower([packet], [()]).items():
                launch_bytes[node] += len(part.pack())
        parts = lower(packets, [()] + [(i - 1,) for i in range(1, launches)])
        for node, part in parts.items():
            # The fixture must exercise instruction payloads, not just movers.
            assert any(
                task.kind == A.KDS_DE_FEED and task.words[2] > 0 for task in part.tasks
            )
            joins_bytes = (launches - 1) * (
                (1 + 3 * (nodes - 1)) * A.OSTPU_TASK_BYTES + 64
            )
            assert len(part.pack()) <= launch_bytes[node] + joins_bytes
    finally:
        for retained in reversed(prepared):
            retained.close()
        dev.close()
