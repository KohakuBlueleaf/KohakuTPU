"""Captured row kernels execute one leased multi-node graph on the device double."""

import json

import numpy as np

from kohakutpu.application.runtime import device as card
from kohakutpu.application.runtime import generated
from kohakutpu.application.runtime.graph.capture import Capture, Library, Span, Template
from kohakutpu.application.runtime.graph.native import NativeLaunch
from kohakutpu.application.runtime.graph.split import SplitRunner
from kohakutpu.stack import rs


def bind_rows(graph, addresses):
    return NativeLaunch(graph, dict(zip(("in0", "out"), addresses, strict=True)), "dsl")


def test_dependent_split_rows_use_only_their_producers(monkeypatch):
    rows, width = 64, 32
    values = (np.arange(rows * width) % 97 - 48).astype(np.float16).reshape(rows, width)
    raw = values.tobytes()
    outputs = []
    for count in (1, 4):
        dev = card.Device(kind="double", board="multimesh_v9t8", nodes=count)
        prepared = None
        try:
            addresses = {name: dev.alloc(len(raw)) for name in ("x", "y", "z")}
            dev.write(addresses["x"], raw)
            capture = Capture()
            grants = [tuple(dev.card.nodes)]
            grants.append(grants[0][1:] + grants[0][:1])
            operand = {
                "kind": "full",
                "dtype": "f16",
                "offset": 0,
                "pitch": width,
                "rows": rows,
            }
            for index, (operation, value) in enumerate((("mul", 0.5), ("add", 1.0))):
                arguments = {
                    "e": {
                        "nodes": [
                            {"load": 0},
                            {"const": value},
                            {"op": operation, "args": [0, 1]},
                        ],
                        "root": 2,
                    },
                    "operands": [operand],
                    "R": rows,
                    "W": width,
                    "out": operand,
                }
                source, selection = generated.split_graph(
                    "vector.rows_kernel",
                    json.dumps(arguments),
                    rs.machine_json(dev.machine),
                    None,
                    grants[index],
                    None,
                )
                assert selection["active_nodes"] == count
                graph = json.loads(source)
                src, dst = (("x", "y"), ("y", "z"))[index]
                capture.launch(
                    Template((operation, count), "elementwise", bind_rows),
                    graph,
                    (Span(src, 0, len(raw)),),
                    Span(dst, 0, len(raw)),
                    addresses,
                    lambda packet, dev=dev: dev.run(
                        dev.kernel(json.dumps(packet.graph)), packet.bindings
                    ),
                )
            eager = dev.read(addresses["z"], len(raw))
            semantic = capture.finish((Span("z", 0, len(raw)),), Library())
            prepared = semantic.prepare(SplitRunner(dev), addresses)
            middle = dev.mmu.resolve(addresses["y"])[1]
            edges = [
                edge
                for edge in prepared.events
                if any(
                    middle <= region["lo"] < region["hi"] <= middle + len(raw)
                    for region in edge["regions"]
                )
            ]
            assert {(e["producer"], e["consumer"]) for e in edges} == set(
                zip(grants[0], grants[1], strict=True)
            )
            for edge in edges:
                rank = grants[0].index(edge["producer"])
                lo = middle + rank * len(raw) // count
                hi = middle + (rank + 1) * len(raw) // count
                assert all(lo <= r["lo"] < r["hi"] <= hi for r in edge["regions"])
                if count == 4:
                    assert 9 <= edge["slot"] < 16
                else:
                    assert edge["slot"] is None
            pending = []
            submit = dev.card.submit_launch

            def published(*args, submit=submit, pending=pending):
                result = submit(*args)
                pending.append(result)
                return result

            monkeypatch.setattr(dev.card, "submit_launch", published)
            prepared.submit()
            dev.write(addresses["y"], bytes([0xFF]) * len(raw))
            dev.write(addresses["z"], bytes([0xFF]) * len(raw))
            prepared.submit()
            result = dev.read(addresses["z"], len(raw))
            assert result == eager
            outputs.append(result)
            for invocation in pending:
                assert invocation.order == [
                    *(("publish", n) for n in dev.card.nodes),
                    *(("collect", n) for n in dev.card.nodes),
                ]
        finally:
            if prepared is not None:
                prepared.close()
            dev.close()
    assert outputs[1] == outputs[0]
