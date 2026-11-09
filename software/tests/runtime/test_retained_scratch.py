"""A retained program must reuse eager scratch, not reserve every launch at once.

This is a value/lifetime witness, not a graph-shape assertion. Eager capture
uses the real compiler and allocator. Replay executes the composed L1 on the
runtime double, after real artifact binding and receiving-board admission.
No hardware is needed by this stage.
"""

import copy
import itertools
import json
import struct

import numpy as np
import pytest

from kohakutpu.application.runtime import generated
from kohakutpu.application.runtime import packages as B
from kohakutpu.application.runtime.device import Device
from kohakutpu.application.runtime.graph import split_recorded
from kohakutpu.application.runtime.graph.native import NativeLaunch
from kohakutpu.application.runtime.graph.split import SplitRunner
from kohakutpu.application.runtime.graph.storage import GraphScratchRefused
from kohakutpu.application.runtime.os import abi as A
from kohakutpu.application.runtime.os.graph import Graph
from kohakutpu.stack import rs


def scratch_copy(dev, size, space):
    """Copy distinct lines on every node through a much larger private arena."""
    graph = copy.deepcopy(
        generated.template_graph(
            "convert.copy_kernel", {"nbytes": 256 * dev.nodes}, dev.machine
        )
    )
    graph["name"] = f"scratch_copy_{space}_{size}"
    template = graph["body"][0]
    graph["body"], graph["scratch"] = [], []
    for node in dev.card.nodes:
        name = f"tmp{node}"
        graph["scratch"].append(
            {
                "name": name,
                "spec": {
                    "shape": [size // 32, 16],
                    "dtype": "f16",
                    "space": space,
                    "layout": [["node", {"int": node}]] if space == "stage" else [],
                },
            }
        )
        incoming, outgoing = copy.deepcopy(template), copy.deepcopy(template)
        external = [{"run": [node * 8, 8]}, "all"]
        private = {"buffer": name, "axes": [{"run": [0, 8]}, "all"]}
        incoming["src"]["axes"] = external
        incoming["dst"] = copy.deepcopy(private)
        outgoing["src"] = copy.deepcopy(private)
        outgoing["dst"]["axes"] = external
        graph["body"].append(
            {
                "kind": "par",
                "var": f"owner{node}",
                "lo": 0,
                "hi": 1,
                "unit": {"kind": "node", "index": node},
                "body": [incoming, outgoing],
                "where": "retained-scratch-witness",
            }
        )
    return graph


def units_of(artifact):
    coords = set()
    for part in artifact.parts:
        h = struct.unpack_from(A.PKG_HDR_FMT, part.package)
        for index in range((h[A.KA_PH_COUNTS2] >> 32) & 65535):
            word = struct.unpack_from(
                "<Q", part.package, h[A.KA_PH_OFF_UNIT] + 16 * index
            )[0]
            coords.add(((word >> 16) & 255, word & 255, (word >> 8) & 255))
    return {coord: index for index, coord in enumerate(sorted(coords))}


@pytest.mark.parametrize("board", ["multimesh_v9t6", "multimesh_v9t8"])
@pytest.mark.parametrize("nodes", [1, 2, 4])
@pytest.mark.parametrize("space", ["stage", "dram"])
def test_retained_launches_reuse_the_capture_window_and_replay_new_values(
    monkeypatch, board, nodes, space
):
    monkeypatch.setenv("KTPU_GRAPH_FUSE", "0")
    dev = Device(kind="double", board=board, nodes=nodes)
    log = None
    try:
        # Unlike the cheap L2-only double, capture must pay the real physical
        # scratch layout and pin it, exactly as the firmware path does.
        monkeypatch.setattr(
            dev.card,
            "compile_graph",
            lambda source: dict(
                rs.compile(source, json.loads(source)["name"], dev.machine)
            ),
        )
        sizes = (640 << 10, 768 << 10, 512 << 10)
        staging = min(a.hi - a.lo for a in dev.memory.staging.values())
        assert max(sizes) < staging < sum(sizes)
        values = np.arange(128 * nodes, dtype=np.float16)
        addresses = [dev.alloc(values.nbytes) for _ in range(4)]
        compiled = [
            dev.kernel(json.dumps(scratch_copy(dev, size, space))) for size in sizes
        ]
        dev.write(addresses[0], values.tobytes())
        dev.record()
        for index, kernel in enumerate(compiled):
            dev.run(
                kernel,
                {"src": addresses[index], "dst": addresses[index + 1]},
                info={"active_nodes": nodes},
            )
        log = dev.recorded()
        before = {key: (address, size) for key, (address, size) in dev.windows.items()}
        arena_used = tuple(a.used for a in dev.memory.arenas())
        if space == "stage":
            assert all(a.largest == 0 for a in dev.memory.staging.values())
        admitted = []
        admit = B.admit

        def checked(raw, machine, node=0, **options):
            admitted.append(node)
            return admit(raw, machine, node, **options)

        monkeypatch.setattr(B, "admit", checked)
        # Exercise the same preparer even for P=1; the double has no real queue
        # and therefore does not select a retained hardware transport itself.
        log.split = split_recorded.prepare(log)
        ((_, prepared),) = log.split.segments
        artifact = prepared.bind_artifact()
        assert set(admitted) == set(range(nodes))
        assert dev.windows == before
        assert tuple(a.used for a in dev.memory.arenas()) == arena_used
        # The same admitted node bytes must also lower through the KAOS adapter.
        lowered = Graph.package_parts(artifact, units_of(artifact))
        assert set(lowered) == set(range(nodes))
        assert all(graph.tasks for graph in lowered.values())
        for increment in (7, 19):
            # Later inputs arrive through the stable capture input slot. Change
            # all values and poison intermediates to catch stale input/results.
            current = values + np.float16(increment)
            dev.write(addresses[0], current.tobytes())
            for address in addresses[1:]:
                dev.write(address, b"\xff" * values.nbytes)
            runs = dev.replay(log)
            assert len(runs) == 1
            assert dev.read(addresses[-1], values.nbytes) == current.tobytes()
            assert prepared.bind_artifact() is artifact
            assert dev.windows == before
            assert tuple(a.used for a in dev.memory.arenas()) == arena_used
    finally:
        if log is not None:
            log.close()
        dev.close()


def test_retained_growth_refuses_before_allocating_or_publishing():

    dev = Device(kind="offline", board="multimesh_v9t8", nodes=1)
    prepared = None
    try:
        x, y = dev.alloc(256), dev.alloc(256)
        graph = scratch_copy(dev, 768 << 10, "stage")
        prepared = SplitRunner(dev).prepare(
            [NativeLaunch(graph, {"src": x, "dst": y}, "copy")], [()]
        )
        prepared.scratch_limits = {("stage", 0): 512 << 10}
        used, epoch = dev.memory.staging[0].used, dev.card.publication_epoch
        with pytest.raises(GraphScratchRefused, match="eager capture envelope"):
            prepared.bind_artifact()
        assert dev.memory.staging[0].used == used
        assert dev.card.publication_epoch == epoch
        assert prepared.artifact is None
    finally:
        if prepared is not None:
            prepared.close()
        dev.close()


def test_bound_retained_graph_is_not_limited_by_a_relocation_table():

    dev = Device(kind="double", board="multimesh_v9t8", nodes=1)
    prepared = None
    try:
        # v9's relocatable format permits only 64 buffers. A physically bound
        # graph is not relocatable and must not build that intermediate table.
        addresses = [dev.alloc(256) for _ in range(71)]
        graph = generated.template_graph(
            "convert.copy_kernel", {"nbytes": 256}, dev.machine
        )
        packets = [
            NativeLaunch(graph, {"src": a, "dst": b}, "copy")
            for a, b in itertools.pairwise(addresses)
        ]
        dependencies = [()] + [(i - 1,) for i in range(1, len(packets))]
        prepared = SplitRunner(dev).prepare(packets, dependencies)
        artifact = prepared.bind_artifact()
        for part in artifact.parts:
            header = struct.unpack_from(A.PKG_HDR_FMT, part.package)
            assert header[A.KA_PH_COUNTS] >> 32 == 0
        for value in (b"a", b"z"):
            dev.write(addresses[0], value * 256)
            dev.write(addresses[-1], bytes(256))
            dev.collect_through(prepared.submit())
            assert dev.read(addresses[-1], 256) == value * 256
            assert prepared.bind_artifact() is artifact
    finally:
        if prepared is not None:
            prepared.close()
        dev.close()
