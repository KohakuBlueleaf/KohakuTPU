"""Fast ring class admission, logical tails and query-local score storage."""

import json
from itertools import product

import numpy as np
import pytest

from kohakutpu.application.kernels.native import attention as A
from kohakutpu.application.kernels.native import conv as C
from kohakutpu.application.kernels.native import generated as G
from kohakutpu.application.kernels.native import layout as Y
from kohakutpu.application.kernels.native.library import admit_pair
from kohakutpu.application.runtime.class_library import ClassLibrary
from kohakutpu.application.runtime.device import Device
from kohakutpu.stack import rs
from kohakutpu.stack.kernel import dsl_machine


def test_loop10_fast_attn_compact_mlp_and_balanced_norm():
    m2 = Device("offline", board="vlt_v9t6_2n", nodes=1).machine
    m2_json = rs.machine_json(m2)
    mlp_compact = G.mlp(197, 768, 3072, 768, m2, "gelu", compact_mlp=True).graph
    mlp_legacy = G.mlp(197, 768, 3072, 768, m2, "gelu", compact_mlp=False).graph
    assert "c_h2" not in {s["name"] for s in mlp_compact["scratch"]}
    assert "c_h2" in {s["name"] for s in mlp_legacy["scratch"]}
    norm_bal = rs._rs.compile(
        json.dumps(G.rows_graph("layernorm", 33, 768, m2, 1e-6, balance_norm=True)),
        m2_json,
        cache_dir="",
        fetch=True,
    )["l1"]
    norm_unbal = rs._rs.compile(
        json.dumps(G.rows_graph("layernorm", 33, 768, m2, 1e-6, balance_norm=False)),
        m2_json,
        cache_dir="",
        fetch=True,
    )["l1"]
    assert norm_bal.count("send vc[0]") == norm_bal.count("send vc[1]") == 31
    assert norm_unbal.count("send vc[0]") == 40 and norm_unbal.count("send vc[1]") == 22


def test_wide_norm_folds_partial_statistics(monkeypatch):
    dev = Device("offline", board="vlt_v9t6s_1n")
    try:
        source = G.rows_graph("layernorm", 16, 768, dev.machine)
        stats = [s for s in source["scratch"] if s["name"] in ("mean", "rstd")]
        assert [s["spec"]["shape"] for s in stats] == [[16], [16]]
        assert all(s["spec"]["space"] == "stage" for s in source["scratch"])
        output = rs._rs.compile(
            json.dumps(source), rs.machine_json(dev.machine), cache_dir="", fetch=True
        )
        assert not output["verdict"]["refused"]
        qkv = G.qkv_projection(1, 16, 768, dev.machine, eps=1e-6).graph
        assert [p["spec"]["shape"] for p in qkv["ports"] if p["role"] == "out"] == [
            [192, 64],
            [192, 64],
            [768, 16],
        ]
        nl = G.norm_linear(197, 768, 2304, dev.machine, 1e-6).graph
        nl_out = rs._rs.compile(
            json.dumps(nl), rs.machine_json(dev.machine), cache_dir="", fetch=True
        )
        assert not nl_out["verdict"]["refused"]
        assert all(a["space"] == "stage" for a in nl_out["scratch"]), nl_out["scratch"]
        assert nl_out["scratch"][0]["bytes"] <= 1048576
        original = G.LR.wide_fold_kernel

        def unscaled_mean(e, operands, r, w, out, cores):
            return original(G.X.Reduce("sum", G.X.Load(0)), operands, r, w, out, cores)

        monkeypatch.setattr(G.LR, "wide_fold_kernel", unscaled_mean)
        mutant = G.rows_graph("layernorm", 16, 768, dev.machine)
        with pytest.raises(AssertionError):
            assert mutant["body"] == source["body"]
    finally:
        dev.close()


def test_ring_class_two_shapes_two_addresses(tmp_path, monkeypatch):
    dev = Device(
        "offline",
        board="vlt_v9t6s_1n",
        library=tmp_path,
        library_only=True,
        classes=True,
    )
    try:
        machine = rs.machine_json(dev.machine)
        graphs = [
            G.linear(m, 64, 32, dev.machine, "relu", row_capacity=16).graph
            for m in (8, 16)
        ]
        identity, _outputs, derived = admit_pair(tmp_path, machine, graphs)
        wrong = [
            rs._rs.compile(
                json.dumps(G.linear(m, 64, 32, dev.machine, "relu").graph),
                machine,
                cache_dir="",
                fetch=True,
            )["packages"][0]["bytes"]
            for m in (8, 16)
        ]
        with pytest.raises(ValueError, match="structure"):
            rs._rs.template_derive(
                wrong,
                json.dumps([{"member": 0}, {"member": 1}]),
                json.dumps({"member": {"lo": 0, "hi": 1}}),
                machine,
            )
        print(identity, len(derived["bytes"]), derived["template_json"])

        def forbidden(*a, **kw):
            raise AssertionError("compiler/exact lookup reached")

        monkeypatch.setattr(rs._rs, "compile", forbidden)
        monkeypatch.setattr(rs._rs, "library_get", forbidden)
        monkeypatch.setattr(rs._rs, "library_prebuild", forbidden)
        for graph in graphs:
            source = json.dumps(graph)
            output = ClassLibrary(tmp_path, machine).lookup(source)
            assert output["packages"][0]["bytes"] == derived["bytes"]
            addresses = []
            for _ in range(2):
                compiled = dev.kernel(source)
                bindings = {n: dev.alloc(s.nbytes()) for n, s in compiled.params}
                addresses.append(bindings)
                dev.run(compiled, bindings)
            assert addresses[0] != addresses[1]
        assert dev.counters["class_hits"] == 4
        assert dev.counters["compile_calls"] == 0
        assert dev.counters["exact_hits"] == 0
        assert (
            len(
                [p for p in tmp_path.glob("*/classes/*.json") if p.name != "index.json"]
            )
            == 1
        )
    finally:
        dev.close()


def test_conv_unfold_walk_has_independent_index_witness():
    machine = Device("offline", board="vlt_v9t6s_1n").machine
    trace = C.unfold_kernel(2, 6, 6, 16, 3, 3, 1, 0).trace(
        {"x": (72, 16)}, machine=dsl_machine(machine)
    )
    x = np.arange(2 * 6 * 6 * 16, dtype=np.int64).reshape(2, 6, 6, 16)
    copy_stmt = next(s for s in trace.graph["body"] if s.get("op") == "copy")

    def gather(stmt):
        walk = dict(stmt["kw"])["walk"]["walk"]
        indices = [
            sum(i * s // 2 for i, (_, s) in zip(combo, walk)) + lane
            for combo in product(*(range(n) for n, _ in walk))
            for lane in range(16)
        ]
        return x.reshape(-1)[indices].reshape(2, 4, 4, 3, 3, 16)

    expected = np.stack(
        [x[:, oy : oy + 3, ox : ox + 3, :] for oy in range(4) for ox in range(4)],
        axis=1,
    ).reshape(2, 4, 4, 3, 3, 16)
    np.testing.assert_array_equal(gather(copy_stmt), expected)
    wrong = json.loads(json.dumps(copy_stmt))
    dict(wrong["kw"])["walk"]["walk"][3][1] -= 32
    with pytest.raises(AssertionError):
        np.testing.assert_array_equal(gather(wrong), expected)
    fused = C.kernel(1, 14, 14, 16, 32, 5, 5, machine, activation="relu").graph
    output = rs._rs.compile(
        json.dumps(fused), rs.machine_json(machine), cache_dir="", fetch=True
    )
    assert not output["verdict"]["refused"]
    assert all(a["space"] == "stage" for a in output["scratch"])
    c1_b8 = C.kernel(
        8, 28, 28, 16, 32, 5, 5, machine, padding=2, activation="relu"
    ).graph
    c1_out = rs._rs.compile(
        json.dumps(c1_b8), rs.machine_json(machine), cache_dir="", fetch=True
    )
    assert not c1_out["verdict"]["refused"], c1_out["verdict"]
    c2_bwd = C.kernel(
        8, 10, 10, 32, 32, 5, 5, machine, padding=4, activation="none"
    ).graph
    c2_bwd_out = rs._rs.compile(
        json.dumps(c2_bwd), rs.machine_json(machine), cache_dir="", fetch=True
    )
    assert not c2_bwd_out["verdict"]["refused"], c2_bwd_out["verdict"]
    assert all(a["space"] == "stage" for a in c2_bwd_out["scratch"]), c2_bwd_out[
        "scratch"
    ]
    assert c2_bwd_out["scratch"][0]["bytes"] <= 1048576
    base_mlp = G.mlp(197, 768, 3072, 768, machine, "gelu").graph
    mlp_out = rs._rs.compile(
        json.dumps(base_mlp), rs.machine_json(machine), cache_dir="", fetch=True
    )
    assert not mlp_out["verdict"]["refused"], mlp_out["verdict"]
    assert all(a["space"] == "stage" for a in mlp_out["scratch"]), mlp_out["scratch"]
    assert mlp_out["scratch"][0]["bytes"] <= 1048576


def test_two_mg_all_fused_ops_split_across_units(monkeypatch):
    m2 = Device("offline", board="vlt_v9t6_2n", nodes=1).machine
    m2_json = rs.machine_json(m2)
    compiled_l1 = {}
    for name, g2 in (
        ("norm_linear", G.norm_linear(32, 768, 2304, m2, 1e-6).graph),
        (
            "projection_attention",
            Y.attention_kernel(
                1, 197, 4, 64, 2, query_blocked=True, space="stage", machine=m2
            ).graph,
        ),
        ("linear", G.linear(197, 768, 768, m2).graph),
        (
            "linear_vq",
            G.linear(32, 768, 768, m2, quant_backend="vector_mx7").graph,
        ),
        ("mlp", G.mlp(36, 768, 3072, 768, m2, "gelu").graph),
        (
            "conv2d",
            C.kernel(8, 14, 14, 16, 32, 5, 5, m2, activation="relu").graph,
        ),
        ("norm_qkv", G.qkv_projection(1, 16, 192, m2, eps=1e-6).graph),
    ):
        out2 = rs._rs.compile(json.dumps(g2), m2_json, cache_dir="", fetch=True)
        assert not out2["verdict"]["refused"], (name, out2["verdict"])
        data = [
            a
            for a in out2["scratch"]
            if not all(n.startswith("__mx7_status") for n, _ in a["members"])
        ]
        assert all(a["space"] == "stage" for a in data), (name, out2["scratch"])
        assert data[0]["bytes"] <= 1048576, (name, out2["scratch"])
        for u in ("send mg[0]", "send mg[1]", "send vc[0]", "send vc[1]"):
            assert u in out2["l1"], (name, u)
        compiled_l1[name] = out2["l1"]
    m1 = Device("offline", board="vlt_v9t6s_1n", nodes=1).machine
    out1 = rs._rs.compile(
        json.dumps(G.linear(32, 192, 192, m1).graph),
        rs.machine_json(m1),
        cache_dir="",
        fetch=True,
    )
    with pytest.raises(AssertionError):
        assert "send mg[1]" in out1["l1"]


def test_logical_key_tail_is_a_mask_operand():
    logical = A.kernel(1, 208, 208, 64, valid_keys=197)
    assert logical.inputs[-1] == "tail"
    with pytest.raises(AssertionError):
        assert A.kernel(1, 208, 208, 64, valid_keys=208).inputs[-1] == "tail"


def test_query_block_bounds_score_storage_and_tail_mask():
    dev = Device("offline", board="vlt_v9t6s_1n")
    try:
        graphs = [
            Y.attention_kernel(1, 197, 3, 64, 1, query_blocked=q)
            .trace(
                {"x": (197, 576), "mask": (2, 128), "tail": (16,)},
                machine=dsl_machine(dev.machine),
            )
            .graph
            for q in (False, True)
        ]

        def score_rows(g):
            return max(
                s["spec"]["shape"][0]
                for s in g["scratch"]
                if s["spec"]["dtype"] == "f16"
                and s["spec"]["shape"][1:] == [256]
                and s["spec"]["layout"]
            )

        assert score_rows(graphs[1]) == 16
        with pytest.raises(AssertionError):
            assert score_rows(graphs[0]) == 16
        for graph in graphs:
            output = rs._rs.compile(
                json.dumps(graph),
                rs.machine_json(dev.machine),
                cache_dir="",
                fetch=True,
            )
            assert not output["verdict"]["refused"]
            print(
                "attention scratch",
                output["scratch"],
                len(output["packages"][0]["bytes"]),
            )
    finally:
        dev.close()
