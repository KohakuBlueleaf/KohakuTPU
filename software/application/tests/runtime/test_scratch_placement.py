"""Whole-footprint staging admission and exact library identity witnesses."""

import json
from unittest.mock import patch

import pytest

from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.graph import storage
from kohakutpu.stack import rs


def test_stage_graph_capacity_and_mutation(monkeypatch):
    graph = dict(
        scratch=[
            dict(
                name="a", spec=dict(dtype="f16", shape=[256], layout=[], space="dram")
            ),
            dict(
                name="b", spec=dict(dtype="f32", shape=[64], layout=[], space="stage")
            ),
        ]
    )

    def witness():
        assert (
            storage.stage_graph(graph, rs.machine_json("multimesh_v9t6"), 767) is graph
        )
        got = storage.stage_graph(graph, rs.machine_json("multimesh_v9t6"), 768)
        assert [p["spec"]["space"] for p in got["scratch"]] == ["stage", "stage"]
        assert graph["scratch"][0]["spec"]["space"] == "dram"
        wide = json.loads(rs.machine_json("multimesh_v9t6"))
        wide["machine"]["mg"]["wburst"] = 64
        assert storage.stage_graph(graph, json.dumps(wide), 4095) is graph
        assert storage.stage_graph(graph, json.dumps(wide), 4096) is not graph

    witness()
    monkeypatch.setattr(storage, "stage_graph", lambda graph, machine, budget: graph)
    with pytest.raises(AssertionError):
        witness()


def test_stage_placement_rejects_unqualified_multinode(monkeypatch):
    dev = D.Device("offline", board="multimesh_v9t6", nodes=2)
    try:
        monkeypatch.setenv("KTPU_LOCAL_SCRATCH", "1")
        with pytest.raises(ValueError, match="single-node"):
            dev.kernel(json.dumps(dict(scratch=[])))
    finally:
        dev.close()


def test_device_staging_precedes_library_key(monkeypatch):
    graph = dict(
        scratch=[
            dict(name="a", spec=dict(dtype="f16", shape=[16], layout=[], space="dram"))
        ]
    )

    def witness():
        dev = D.Device("offline", board="multimesh_v9t6")
        try:
            with (
                patch.object(dev, "compile_graph", return_value={}) as compile_graph,
                patch.object(
                    D.generated, "from_output", side_effect=lambda *a: object()
                ),
            ):
                monkeypatch.setenv("KTPU_LOCAL_SCRATCH", "0")
                a = dev.kernel(json.dumps(graph))
                monkeypatch.setenv("KTPU_LOCAL_SCRATCH", "1")
                b = dev.kernel(json.dumps(graph))
                assert a is not b
                assert dev.kernel(json.dumps(graph)) is b
                assert [
                    json.loads(c.args[0])["scratch"][0]["spec"]["space"]
                    for c in compile_graph.call_args_list
                ] == ["dram", "stage"]
        finally:
            dev.close()

    witness()
    monkeypatch.setattr(D, "stage_graph", lambda graph, machine, budget: graph)
    with pytest.raises(AssertionError):
        witness()
