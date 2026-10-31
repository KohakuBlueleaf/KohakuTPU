"""Native operator integration with the shared Rust ring generator."""

import json
from itertools import product

import numpy as np
import pytest

from kohakutpu.application.kernels.native import generated as G
from kohakutpu.application.kernels.native import layout as Y
from kohakutpu.application.kernels.native.composition import statements
from kohakutpu.application.runtime.device import Device
from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.stack import rs
from kohakutpu.stack.kernel import dsl_machine


def test_projection_layout_walk_has_independent_index_witness():
    machine = OfflineCard("vlt_v9t6s_1n").machine
    trace = Y.split_kernel(2, 16, 3, 64, 1).trace(
        {"x": (32, 576), "mask": (2, 128)}, machine=dsl_machine(machine)
    )
    x = np.arange(2 * 16 * 3 * 3 * 64, dtype=np.int64).reshape(2, 16, 3, 3, 64)
    copies = [s for s in trace.graph["body"] if s.get("op") == "copy"][:3]

    def gather(stmt):
        attrs = dict(stmt["kw"])
        walk = attrs["walk"]["walk"]
        start = attrs["at"]["int"] // 2
        indices = [
            start + sum(i * s // 2 for i, (_, s) in zip(combo, walk)) + lane
            for combo in product(*(range(n) for n, _ in walk))
            for lane in range(16)
        ]
        return x.reshape(-1)[indices].reshape(6, 16, 64)

    for i, statement in enumerate(copies):
        np.testing.assert_array_equal(
            gather(statement), x[:, :, i].transpose(0, 2, 1, 3).reshape(6, 16, 64)
        )
    wrong = json.loads(json.dumps(copies[0]))
    dict(wrong["kw"])["walk"]["walk"][1][1] *= 3
    with pytest.raises(AssertionError):
        np.testing.assert_array_equal(
            gather(wrong), x[:, :, 0].transpose(0, 2, 1, 3).reshape(6, 16, 64)
        )


def test_generated_mlp_exact_library_two_addresses(tmp_path, monkeypatch):
    dev = Device("offline", board="vlt_v9t6s_1n", library=tmp_path, library_only=True)
    try:
        graph = G.mlp(8, 64, 64, 32, dev.machine, "relu").graph
        source = json.dumps(graph)
        rs._rs.library_prebuild(str(tmp_path), source, rs.machine_json(dev.machine))

        def forbidden(*a, **kw):
            raise AssertionError("runtime compile/prebuild reached")

        monkeypatch.setattr(rs._rs, "compile", forbidden)
        monkeypatch.setattr(rs._rs, "library_prebuild", forbidden)
        address_sets = []
        for _ in range(2):
            compiled = dev.kernel(source)
            bindings = {n: dev.alloc(s.nbytes()) for n, s in compiled.params}
            address_sets.append(bindings)
            dev.run(compiled, bindings)
        assert address_sets[0] != address_sets[1]
        assert dev.counters["compile_calls"] == 0
        assert dev.counters["exact_hits"] == 2
    finally:
        dev.close()


def test_gelu_mlp_retains_fp16_activation_boundary(monkeypatch):
    machine = OfflineCard("vlt_v9t6s_1n").machine
    graph = G.mlp(197, 192, 768, 192, machine, "gelu").graph
    stores = [
        s
        for s in statements(graph["body"])
        if s.get("kind") == "store" and s["dst"]["buffer"] == "p_out"
    ]
    assert len(stores) == 2
    output = rs._rs.compile(
        json.dumps(graph), rs.machine_json(machine), cache_dir="", fetch=True
    )
    assert not output["verdict"]["refused"]
    assert output["scratch"][0]["space"] == "stage"
    assert output["scratch"][0]["bytes"] <= 1048576
    original = G.renamed

    def drop_activation_stores(value, mapping, field=""):
        got = original(value, mapping, field)
        if isinstance(got, dict) and str(got.get("name", "")).startswith("rows_"):
            got = dict(got, body=[])
        return got

    monkeypatch.setattr(G, "renamed", drop_activation_stores)
    mutant = G.mlp(197, 192, 768, 192, machine, "gelu").graph
    with pytest.raises(AssertionError):
        assert (
            len(
                [
                    s
                    for s in statements(mutant["body"])
                    if s.get("kind") == "store" and s["dst"]["buffer"] == "p_out"
                ]
            )
            == 2
        )


def test_ring_mlp_removes_dram_product_and_keeps_two_quant_stages():
    machine = OfflineCard("vlt_v9t6s_1n").machine
    graphs = [
        G.mlp(8, 192, 768, 192, machine, "relu", ring_depth=d).graph for d in (0, 1, 2)
    ]

    def no_product_dram(graph):
        assert all(
            p["spec"]["space"] == "stage"
            for p in graph["scratch"]
            if p["spec"]["dtype"] == "f16"
        )

    for graph in graphs[1:]:
        no_product_dram(graph)
    with pytest.raises(AssertionError):
        no_product_dram(graphs[0])
    for graph in graphs:
        ops = list(statements(graph["body"]))
        assert sum(s.get("op") == "quant" for s in ops) == 2
        assert sum(s.get("op") == "gemm" for s in ops) == 2
        assert len(graph["ports"]) == 6
        assert len({s["name"] for s in graph["scratch"]}) == len(graph["scratch"])
        output = rs._rs.compile(
            json.dumps(graph), rs.machine_json(machine), cache_dir="", fetch=True
        )
        assert not output["verdict"]["refused"]
        print("ring", len(output["packages"][0]["bytes"]), output["scratch"])
