"""Packing policy, finite-input validation and cache admission witnesses."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from kohakutpu.application.runtime import packages as B
from kohakutpu.application.runtime.card import NodeCard
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.compiler import _rs
from kohakutpu.language import dsl as kt
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def source(side="a", backend=None, finite=True, rows=8, cols=64, groups=2, nk=2):
    opts = (" finite=1" if finite else "") + (f" backend={backend}" if backend else "")
    return (
        "target ktpu.v9\n\n"
        f"fn pack.l2(x: f16[{rows}, {cols}] @dram, q: mx7{side}[{rows}, {cols}] "
        f"@dram(g{'m' if side == 'a' else 'n'}={groups}, nk={nk}))\n"
        f"    quant q <- x{opts}\n"
    )


def test_board_default_and_override():
    m = machine_of("multimesh_v9t6", nodes=1)
    c = K.compile(source(), machine=m)
    assert "vpack v0" in c.l1_text and "mx7a" in c.l1_text
    assert "    quant w(" not in c.l1_text
    with pytest.raises(ValueError, match="excludes"):
        K.compile(source(backend="mover_quant"), machine=m)
    model = machine_of("vlt_v9t6s_1n")
    assert "quant w(" in K.compile(source(), machine=model).l1_text
    assert "vpack v0" in K.compile(source(backend="vector_mx7"), machine=model).l1_text


def test_unproven_and_missing_mx_refuse():
    m = machine_of("multimesh_v9t6", nodes=1)
    checked = K.compile(source(finite=False), machine=m)
    assert "assert_zero" in checked.l1_text
    assert "__mx7_status_" in checked.l1_text
    assert "assert_zero" not in K.compile(source(finite=True), machine=m).l1_text
    doc = json.loads(m.machine_json)
    doc["machine"]["vc"]["has_mx"] = 0
    with pytest.raises(ValueError, match="eight-chunk"):
        K.compile(source(), machine=replace(m, machine_json=json.dumps(doc)))


@pytest.mark.parametrize("slots,cores,looped", [(1, 1, False), (2, 2, True)])
def test_schedule_knobs_and_compact_package(slots, cores, looped):
    m = machine_of(
        "multimesh_v9t6",
        nodes=1,
        packing_slots=slots,
        packing_cores=cores,
        packing_looped=looped,
    )
    c = K.compile(source(rows=272, cols=128, groups=34, finite=False), machine=m)
    assert not c.verdict["refused"], c.verdict
    assert ("label mx7_" in c.l1_text) == looped
    assert ("send vc[1]" in c.l1_text) == (cores == 2)
    assert ("vfill a0 -> l1[192]" in c.l1_text) == (slots == 2)
    assert "assert_zero r14 code=1" in c.l1_text


def test_legacy_policy_does_not_claim_checked_firmware():
    m = machine_of("multimesh_v9t6", nodes=1)
    doc = json.loads(m.machine_json)
    doc["machine"]["conversion"]["fp16_mx7"]["version"] = 1
    with pytest.raises(ValueError, match="validated finite"):
        K.compile(
            source(finite=False), machine=replace(m, machine_json=json.dumps(doc))
        )


def test_large_pitch_uses_compact_node_batch_walk():
    machine = machine_of(
        "vlt_v9t6s_1n", packing_backend="vector_mx7", packing_looped=True
    )
    text = "target ktpu.v9\nfn pack.l2(x: f16[224,32768] @dram, q: mx7a[224,32] @dram(gm=56,nk=1))\n    quant q <- x[0 : +224, 0 : +32]\n"
    compiled = K.compile(text, machine=machine)
    assert "label mx7_batch_" in compiled.l1_text
    assert "r8 = add r8, r10" in compiled.l1_text
    assert not compiled.verdict["refused"]


def test_loop_body_size_is_independent_of_outer_entry_count():
    machine = machine_of(
        "vlt_v9t6s_1n", packing_backend="vector_mx7", packing_looped=True
    )
    sizes = []
    for tiles in (1, 16, 128):
        compiled = K.compile(
            source(rows=224 * tiles, cols=32, groups=56, nk=1, finite=False),
            machine=machine,
        )
        sizes.append(sum(len(p["bytes"]) for p in compiled.packages.values()))
    assert len(set(sizes)) == 1, sizes


@pytest.mark.parametrize(
    "knob,value", [("packing_slots", 3), ("packing_cores", 3), ("packing_looped", 2)]
)
def test_invalid_scheduling_override_refuses(knob, value):
    with pytest.raises(ValueError):
        machine = machine_of(
            "vlt_v9t6s_1n", packing_backend="vector_mx7", **{knob: value}
        )
        K.compile(source(finite=False), machine=machine)


def test_library_identity_includes_conversion_policy():
    m = machine_of("vlt_v9t6s_1n")
    args = {
        "src": {"kind": "full", "dtype": "f16", "offset": 0, "pitch": 64, "rows": 8},
        "rows": 8,
        "cols": 64,
        "rp": 8,
        "cp": 64,
        "side": "a",
        "g": 2,
        "nk": 2,
    }
    graph = _rs.generate("matmul.quant_kernel", json.dumps(args), m.machine_json)
    doc = json.loads(m.machine_json)
    doc["machine"]["conversion"]["fp16_mx7"]["preferred_backend"] = "vector_mx7"
    a = json.loads(_rs.library_key(graph, m.machine_json))
    b = json.loads(_rs.library_key(graph, json.dumps(doc)))
    assert a["machine"] != b["machine"]


def test_restrictive_admission_checks_mover_and_engine():
    permissive = machine_of("vlt_v9t6s_1n")
    c = K.compile(source(), machine=permissive)
    p = c.programs({"x": 0x1000000, "q": 0x2000000})[None]
    doc = json.loads(permissive.machine_json)
    doc["machine"]["conversion"]["fp16_mx7"].update(
        allowed_backends=["vector_mx7"], preferred_backend="vector_mx7"
    )
    restricted = json.dumps(doc)
    for lower in (False, True):
        raw = p.bound(lower)
        _rs.package_admit(raw, permissive.machine_json)
        with pytest.raises(ValueError, match="forbids mover_quant GO"):
            _rs.package_admit(raw, restricted)


def test_replay_and_checker_off_still_use_receiving_policy():
    permissive = machine_of("vlt_v9t6s_1n")
    c = K.compile(source(), machine=permissive)
    raw = c.programs({"x": 0x1000000, "q": 0x2000000})[None].bound(True)
    receiver = OfflineCard("multimesh_v9t6")
    receiver.check = "off"
    with pytest.raises(ValueError, match="forbids mover_quant GO"):
        receiver.replay([("run", {0: (raw, ())})])
    fake = SimpleNamespace(machine_json=receiver.machine_json)
    with pytest.raises(ValueError, match="forbids mover_quant GO"):
        NodeCard._submit(fake, 0, raw, ())


def test_cache_policy_omission_mutant_is_killed(monkeypatch):
    original = _rs.library_key

    def ignore_policy(graph, machine):
        doc = json.loads(machine)
        doc["machine"]["conversion"]["fp16_mx7"].update(preferred_backend="mover_quant")
        return original(graph, json.dumps(doc, sort_keys=True))

    monkeypatch.setattr(_rs, "library_key", ignore_policy)
    with pytest.raises(AssertionError):
        test_library_identity_includes_conversion_policy()


def test_dsl_backend_and_finite_contract_are_serialized():
    m = machine_of("vlt_v9t6s_1n")

    def body(x, q):
        kt.quant(q, x, finite=True, backend="vector_mx7")

    kernel = kt.Kernel.build(
        "direct_pack",
        body,
        {
            "x": kt.In(8, 64),
            "q": kt.Out(8, 64, dtype="mx7a", layout=dict(gm=2, nk=2)),
        },
    )
    given = {
        "x": kt.TensorSpec.make((8, 64), "f16"),
        "q": kt.TensorSpec.make((8, 64), "mx7a", dict(gm=2, nk=2)),
    }
    c = K.specialise(kernel, given, machine=m)
    assert "vpack v0" in c.l1_text


def test_override_is_machine_keyed_and_cannot_widen_board():
    mover = machine_of("vlt_v9t6s_1n", packing_backend="mover_quant")
    vector = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    assert mover.machine_json != vector.machine_json
    with pytest.raises(ValueError, match="excludes"):
        machine_of("multimesh_v9t6", packing_backend="mover_quant")


def test_mover_only_board_admits_no_vector_pack():
    m = machine_of("vlt_v9t6s_1n")
    c = K.compile(source(backend="vector_mx7"), machine=m)
    p = c.programs({"x": 0x1000000, "q": 0x2000000})[None]
    doc = json.loads(m.machine_json)
    doc["machine"]["conversion"]["fp16_mx7"]["allowed_backends"] = ["mover_quant"]
    for lower in (False, True):
        with pytest.raises(ValueError, match="forbids vector_mx7"):
            _rs.package_admit(p.bound(lower), json.dumps(doc))


def test_completion_wait_mutant_is_killed_by_ordering_property():
    m = machine_of(
        "vlt_v9t6s_1n", packing_slots=1, packing_cores=1, packing_looped=False
    )
    text = source(backend="vector_mx7", rows=128, groups=32)
    with rs.mutant("l2.packing/no-drain-wait"):
        c = K.compile(text, machine=m)
    findings = c.verdict["findings"]
    assert any(f["kind"] == "race" and "VDRAIN" in f["text"] for f in findings)


def test_prepared_replay_admits_each_package_before_pinning_it():
    # Prepared submissions skip per-replay admission: preparation is the gate.
    permissive = machine_of("vlt_v9t6s_1n")
    c = K.compile(source(), machine=permissive)
    raw = c.programs({"x": 0x1000000, "q": 0x2000000})[None].bound(True)
    pinned = []
    queue = SimpleNamespace(prepared_bytes=0, prepare=pinned.append)
    fake = SimpleNamespace(
        machine_json=OfflineCard("multimesh_v9t6").machine_json, queues={0: queue}
    )
    with pytest.raises(ValueError, match="forbids mover_quant GO"):
        NodeCard.prepare_recorded(fake, [("run", {0: (raw, ())})], 1 << 24)
    assert pinned == []


def test_build_admits_for_the_receiving_machine():
    # The card's eager path submits what `built` admitted, without a second check.
    p = K.compile(source(), machine=machine_of("vlt_v9t6s_1n")).programs(
        {"x": 0x1000000, "q": 0x2000000}
    )[None]
    B.built({0: p}, machine_of("vlt_v9t6s_1n"), p.fetch)
    with pytest.raises(ValueError, match="forbids mover_quant GO"):
        B.built({0: p}, machine_of("multimesh_v9t6", nodes=1), p.fetch)


def test_one_launch_binds_and_admits_once(monkeypatch):
    # The runtime validates a launch's bind and the card builds it again; the
    # card path admits it again with explicit empty bindings: one Rust call each.
    m = machine_of("vlt_v9t6s_1n")
    p = K.compile(source(), machine=m).programs({"x": 0x1000000, "q": 0x2000000})[None]
    # A compile cache can hand back a package other tests already bound.
    p.__dict__.pop("_bound", None)
    B._admit.cache_clear()
    binds, admits = [], []
    real_bind, real_admit = rs.bind, _rs.package_admit
    monkeypatch.setattr(
        rs, "bind", lambda *a, **k: binds.append(a) or real_bind(*a, **k)
    )
    monkeypatch.setattr(
        _rs, "package_admit", lambda *a: admits.append(a) or real_admit(*a)
    )
    low = p.bound(True)
    assert p.bound(True) is low and len(binds) == 1
    p.bound(False)
    assert len(binds) == 2
    B.admit(low, m.machine_json, 0)
    B.admit(low, m.machine_json, 0, ())
    assert len(admits) == 1
