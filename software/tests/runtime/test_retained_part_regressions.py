"""Narrow controls for the round-2 gate regressions; not a compile-work witness."""

import json
import struct
from types import SimpleNamespace

from kohakutpu.application.runtime.graph import split as split_module
from kohakutpu.application.runtime.graph.native import NativeLaunch
from kohakutpu.application.runtime.graph.parts import BoundGraphSequence
from kohakutpu.application.runtime.os import abi as A
from kohakutpu.application.runtime.split import (
    BoundGraphArtifact,
    GraphArtifact,
    NodePart,
    RetainedArtifact,
)


def _empty_package():
    # A header and one END; no units, relocations or buffers. These controls
    # inspect metadata only; the old runtime tests execute real row packages.
    header = [0] * (struct.calcsize(A.PKG_HDR_FMT) // 8)
    header[A.KA_PH_MAGIC] = (
        A.KA_PKG_MAGIC | A.KA_PKG_VERSION << 32 | A.KA_PKG_HEADER << 48
    )
    header[A.KA_PH_SIZE] = A.KA_PKG_HEADER + 32
    header[A.KA_PH_COUNTS2] = 1 << 16
    for field in (
        A.KA_PH_OFF_UNIT,
        A.KA_PH_OFF_BUF,
        A.KA_PH_OFF_STEP,
        A.KA_PH_OFF_REL,
        A.KA_PH_OFF_PAY,
    ):
        header[field] = A.KA_PKG_HEADER
    return struct.pack(A.PKG_HDR_FMT, *header) + bytes(32)


def test_trace_only_accounting_compiles_individual_launches_not_the_region(monkeypatch):
    compiled_sources = []
    compose_calls = []
    machine = json.dumps(
        {
            "fw": {
                "max_pkg_units": 8,
                "engine_units": 8,
                "max_units": 8,
                "max_buffers": 4096,
            }
        }
    )
    dev = SimpleNamespace(
        card=SimpleNamespace(machine_json=machine, nodes=(0,)),
        machine=object(),
        home=None,
        kernel=lambda source: SimpleNamespace(
            name=json.loads(source)["name"], packages={}
        ),
        mmu=SimpleNamespace(resolve=lambda at: (None, at)),
        _retain=lambda values, pinned=False: [],
        _node_of=lambda node: 0,
    )

    def compile_one(source, name, target):
        compiled_sources.append(json.loads(source)["name"])
        assert target is dev.machine and name == compiled_sources[-1]
        return {"packages": {0: {"bytes": _empty_package()}}}

    monkeypatch.setattr(split_module.rs, "compile", compile_one)
    monkeypatch.setattr(split_module.rs, "machine_json", lambda target: machine)
    monkeypatch.setattr(
        split_module.rs._rs,
        "compile_split_graph",
        lambda *args, **kw: compose_calls.append(kw) or {"events": []},
    )
    monkeypatch.setattr(
        split_module.generated,
        "from_output",
        lambda *args: SimpleNamespace(name="captured_split", scratch=[]),
    )
    packets = [
        NativeLaunch(
            {
                "name": f"launch{i}",
                "format": "ktpu.graph",
                "version": 1,
                "target": "test",
                "machine": {},
                "consts": {},
                "ports": [],
                "scratch": [],
                "body": [],
            },
            {},
            "dsl",
        )
        for i in range(2)
    ]
    prepared = split_module.SplitRunner(dev).prepare(packets, [(), (0,)])
    assert compiled_sources == ["launch0", "launch1"]
    assert len(compose_calls) == 1
    assert len(compose_calls[0]["launches"]) == 2
    assert "part_nodes" not in compose_calls[0]
    assert prepared.planned_parts == ()
    assert prepared.dependencies == ((), (0,))


def test_sequence_manifest_preserves_the_object_and_read_only_artifact_contract():
    raw = _empty_package()
    stages = tuple(
        BoundGraphArtifact(
            "machine",
            (NodePart(0, raw),),
            manifest=json.dumps({"dependencies": [[i, []]], "events": []}).encode(),
        )
        for i in range(2)
    )
    provenance = json.dumps({"dependencies": [[], [0]], "events": []}).encode()
    sequence = BoundGraphSequence(stages, ((0, 1), (1, 2)), provenance)
    graph: GraphArtifact = sequence
    retained: RetainedArtifact = sequence
    manifest = json.loads(graph.manifest)
    assert manifest.get("dependencies") == [[], [0]]
    assert manifest["events"] == []
    assert [part["interval"] for part in manifest["parts"]] == [[0, 1], [1, 2]]
    assert all(isinstance(part["manifest"], dict) for part in manifest["parts"])
    assert retained.package_lists == {0: (raw, raw)}
