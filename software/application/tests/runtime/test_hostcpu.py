"""Exact planning keys, relocation-sensitive admission and library lifetimes."""

import json
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest
from tinygrad.dtype import dtypes
from tinygrad.uop.ops import AxisType, UOp

from kohakuaccel.driver.node.queue import layout as QL
from kohakutpu.application.kernels import expr as X
from kohakutpu.application.ktpugrad import memo as M
from kohakutpu.application.ktpugrad import run as R
from kohakutpu.application.ktpugrad import seam
from kohakutpu.application.ktpugrad.lower import Access, Plan
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import packages as B
from kohakutpu.application.runtime.arena import Arena
from kohakutpu.application.runtime.card import NodeCard
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.compiler import _rs
from kohakutpu.stack import rs


def sample():
    a, b = UOp.range(4, 0, AxisType.LOOP), UOp.range(32, 1, AxisType.LOOP)
    return Plan(
        "vector",
        UOp.sink(),
        "memo_test",
        0,
        dtypes.half,
        (4, 32),
        (a, b),
        expr=X.Load(0),
        accesses=[Access(1, dtypes.half, ((a, 32), (b, 1)), 0)],
    )


def test_plan_key_tracks_shape_stride_table_and_hint(monkeypatch):
    monkeypatch.setenv(M.ENV, "1")
    seen = []

    @M.memo
    def recipe(plan, hints):
        seen.append(1)
        return (plan.out_shape, plan.accesses[0].off, tuple(hints.items()))

    p = sample()
    assert recipe(p, {"producer": "flat"}) is recipe(p, {"producer": "flat"})
    recipe(replace(p, out_shape=(2, 64)), {"producer": "flat"})
    recipe(replace(p, accesses=[replace(p.accesses[0], off=16)]), {"producer": "flat"})
    recipe(p, {"producer": "rows"})
    table = np.array([1, 2], dtype=np.float16)
    p.accesses = [replace(p.accesses[0], slot=None, table=table)]
    recipe(p, {})
    table[0] = 7
    recipe(p, {})
    assert len(seen) == 6
    assert len(recipe.cache) == 6


def test_mutant_plan_key_omits_layout_and_hints_is_caught(monkeypatch):
    real = M.freeze

    def omit(value):
        if isinstance(value, Plan):
            return value.ast
        if isinstance(value, dict):
            return ()
        return real(value)

    monkeypatch.setattr(M, "freeze", omit)
    with pytest.raises(AssertionError):
        test_plan_key_tracks_shape_stride_table_and_hint(monkeypatch)


def test_pure_rows_view_reuses_but_layout_mutation_misses(monkeypatch):
    monkeypatch.setenv(M.ENV, "1")
    R._view.cache.clear()
    p = sample()
    a, b = p.out_ranges
    p.out_ranges, p.reduce_ranges, p.out_shape = (a,), (b,), (4,)
    first = R._view(p)
    assert first[0] == "along" and R._view(p) is first
    p.accesses = [replace(p.accesses[0], coef=((a, 1), (b, 32)))]
    second = R._view(p)
    assert second is not first and second[0] == "down"
    monkeypatch.setenv(M.ENV, "0")
    assert R._view(p) == R._view.__wrapped__(p)


def test_memo_is_bounded_and_no_hash_only_collision(monkeypatch):
    monkeypatch.setenv(M.ENV, "1")
    monkeypatch.setattr(M, "KEPT", 2)

    @M.memo
    def f(value):
        return value

    for n in range(4):
        assert f(n) == n
    assert len(f.cache) == 2
    a, b = X.Const(1), X.Const(2)
    monkeypatch.setattr(M.Identity, "__hash__", lambda self: 0)
    assert f(a) is a and f(b) is b and f(a) is a


def relocation_package(machine):
    transform = json.loads(machine)["machine"]["mover"]["modes"]["transform"]
    calls = [
        ["spans_from", [[0, 32]]],
        [
            "mover",
            [[0x10, 0], [0, (1 << 16) | transform]],
            {"addresses": [[[111, 4, 5]]]},
        ],
        ["end"],
    ]
    return _rs.package_build('{"credit":32}', json.dumps(calls), machine)


def relocation_witness(admit, cached=True):
    machine = machine_of("multimesh_v9t6", nodes=1).machine_json
    raw = relocation_package(machine)
    assert _rs.package_read(raw, machine)[
        "relocs"
    ], "witness must relocate an inspected word"
    admit(raw, machine, bindings=[0], cached_context=cached)
    try:
        admit(raw, machine, bindings=[32], cached_context=cached)
    except ValueError as error:
        assert "forbids mover_quant GO" in str(error)
    else:
        raise AssertionError("a relocation changing the transform id was admitted")


@pytest.mark.parametrize("cached", [False, True])
def test_relocation_changes_inspected_word_and_still_refuses(cached):
    relocation_witness(_rs.package_admit, cached)


def test_mutant_address_independent_admission_is_caught():
    seen = set()

    def stale(raw, machine, **kwargs):
        if (raw, machine) not in seen:
            _rs.package_admit(raw, machine, **kwargs)
            seen.add((raw, machine))

    with pytest.raises(AssertionError, match="transform id"):
        relocation_witness(stale)


def test_admission_context_separates_receiving_policy():
    machine = machine_of("multimesh_v9t6", nodes=1).machine_json
    raw = relocation_package(machine)
    doc = json.loads(machine)
    doc["machine"]["conversion"]["fp16_mx7"].update(
        allowed_backends=["mover_quant", "vector_mx7"], preferred_backend="vector_mx7"
    )
    _rs.package_admit(raw, json.dumps(doc), bindings=[32])
    relocation_witness(_rs.package_admit)
    with pytest.raises(ValueError):
        _rs.package_admit(raw, machine, mesh=100, bindings=[0])


def library_witness(monkeypatch):
    dev = D.Device("offline", board="multimesh_v9t6", library="first")
    monkeypatch.setattr(dev, "compile_graph", lambda source: {"root": dev.library})
    monkeypatch.setattr(D.generated, "from_output", lambda *a: a[2])
    try:
        first = dev.kernel("{}")
        dev.library = "second"
        second = dev.kernel("{}")
        assert second is not first and second["root"] == "second"
        dev.invalidate_library()
        assert dev.kernel("{}") is not second
    finally:
        dev.close()


def test_kernel_cache_library_change_and_explicit_reload(monkeypatch):
    library_witness(monkeypatch)


def test_mutant_library_change_not_invalidated_is_caught(monkeypatch):
    monkeypatch.setattr(D.Device, "_kernel_key", lambda self, *a: a)
    monkeypatch.setattr(
        D.Device,
        "library",
        property(
            lambda self: self._library,
            lambda self, value: setattr(self, "_library", value),
        ),
    )
    with pytest.raises(AssertionError):
        library_witness(monkeypatch)


@pytest.mark.parametrize("fast", [False, True])
def test_arena_lookup_never_outlives_reservation(fast):
    arena = Arena(0, 1024, align=32)
    arena.fast_lookup = fast
    a, b = arena.alloc(64), arena.alloc(64)
    assert arena.containing(a) == a and arena.containing(b + 31) == b
    arena.release(a)
    with pytest.raises(KeyError):
        arena.containing(a)
    assert arena.alloc(32) == a
    with pytest.raises(KeyError):
        arena.containing(a + 32)
    arena.close()
    with pytest.raises(KeyError):
        arena.containing(b)


def test_arena_shortcut_skips_scan_and_undo_is_detected():
    scanned = []

    class Live(dict):
        def items(self):
            scanned.append(1)
            return super().items()

    arena = Arena(0, 128, align=32)
    at = arena.alloc(32)
    arena.live = Live(arena.live)
    arena.fast_lookup = True
    assert arena.containing(at) == at and not scanned
    arena.fast_lookup = False
    arena.containing(at)
    with pytest.raises(AssertionError):
        assert not scanned, "the base lookup scanned the arena"


def _resident_program(machine_json):
    source = (
        "target ktpu.v9\nimage image()\n"
        "    seti s0 = 1\n    halt\n"
        "fn k.l1()\n    send vc[0]\n        run image()\n    barrier\n"
    )
    compiled = _rs.compile(source, machine_json, "k", l1=True, fetch=True, cache_dir="")
    raw = compiled["packages"][0]["bytes"]
    fetch = tuple(json.loads(machine_json)["machine"]["die"]["fetch_port"])
    return B.Prebuilt(raw, (), machine_json, 0, (), False, fetch)


@pytest.mark.parametrize("fast", [False, True])
def test_stateless_submission_elides_snapshots_without_eliding_residency(
    monkeypatch, fast
):
    machine = machine_of("multimesh_v9t6", nodes=2)
    prog = _resident_program(machine.machine_json)
    full_bytes = B.built({0: prog}, machine, prog.fetch, None)[0][1].to_bytes()
    temp_cores = _rs.Cores(machine.machine_json)
    B.built({0: prog}, machine, prog.fetch, {0: temp_cores}, resident=True)
    omitted_bytes = B.built(
        {0: prog}, machine, prog.fetch, {0: temp_cores}, resident=True
    )[0][1].to_bytes()
    assert len(omitted_bytes) < len(full_bytes)

    def offline_cell(mode):
        if mode is None:
            monkeypatch.delenv("KOHAKUTPU_RESIDENT", raising=False)
        else:
            monkeypatch.setenv("KOHAKUTPU_RESIDENT", mode)
        offline = D.OfflineCard("multimesh_v9t6")
        first = offline.run(prog)["pkg"].to_bytes()
        second = offline.run(prog)["pkg"].to_bytes()
        return first, second

    def node_card_cell(mode):
        if mode is None:
            monkeypatch.delenv("KOHAKUTPU_RESIDENT", raising=False)
        else:
            monkeypatch.setenv("KOHAKUTPU_RESIDENT", mode)
        monkeypatch.setenv("KOHAKUTPU_HOST_SUBMIT_CACHE", str(int(fast)))
        card = object.__new__(NodeCard)
        card.nodes, card.debug, card.log = (0, 1), False, None
        card.residents = {
            0: _rs.Cores(machine.machine_json),
            1: _rs.Cores(machine.machine_json),
        }
        card.machine_json = machine.machine_json
        card.machine, card.fetch = machine, prog.fetch
        card.verify = lambda progs: None
        submitted = []
        card._submit = lambda n, raw, bindings, **kw: submitted.append(raw) or len(
            submitted
        )
        B.built({1: prog}, machine, prog.fetch, {1: card.residents[1]}, resident=True)
        card.submit({0: prog})
        card.submit({0: prog})
        peer = B.built(
            {1: prog},
            machine,
            prog.fetch,
            {1: card.residents[1].snapshot()},
            resident=True,
        )[1][1].to_bytes()
        return submitted[0], submitted[1], peer

    def check_all_cells():
        # Unset or "0" sends images whole on every card, whatever the submit
        # cache; only "0" also forgets the peer node's residents.
        for mode in (None, "0", "1", "yes"):
            want_second = full_bytes if mode in (None, "0") else omitted_bytes
            assert offline_cell(mode) == (full_bytes, want_second), (
                "OfflineCard",
                mode,
            )
            want_peer = full_bytes if mode == "0" else omitted_bytes
            assert node_card_cell(mode) == (full_bytes, want_second, want_peer), (
                "NodeCard",
                mode,
                fast,
            )

    check_all_cells()
    for name in ("app.resident/unset_is_off", "app.resident/only_one_resides"):
        assert name in rs.mutants()
        with rs.mutant(name), pytest.raises(AssertionError):
            check_all_cells()


def test_python_admission_cache_tracks_bindings(monkeypatch):
    def admit(raw, machine, *, bindings, cached_context):
        B.admit(raw, machine, 0, bindings, cached=cached_context)

    relocation_witness(admit)


def noengine_witness(monkeypatch, mutant=False):
    admitted = []
    card = object.__new__(NodeCard)
    card.nodes, card.debug, card.log = (0,), True, None
    card.residents = {}
    card.verify = lambda progs: None
    card.machine = card.fetch = None
    card._forget_residents = lambda progs: None
    pkg = SimpleNamespace(to_bytes=lambda: b"plain-wire")
    monkeypatch.setattr(B, "built", lambda *a, **k: {0: (pkg, pkg, ())})
    monkeypatch.setattr(B, "admit", lambda raw, *a, **k: admitted.append(raw))
    monkeypatch.setenv("KOHAKUTPU_HOST_SUBMIT_CACHE", "1")
    monkeypatch.setenv("KOHAKUTPU_RESIDENT", "0")

    def submit(n, raw, bindings, **kw):
        if not kw["admitted"] and not mutant:
            B.admit(raw, "machine", n, bindings)
        return 1

    card._submit = submit
    card.submit(object(), flags=QL.RUN_F_NOENGINE)
    assert admitted == [b"plain-wire"], "NOENGINE skipped receiving-board admission"


def test_noengine_admits_the_selected_wire_stream(monkeypatch):
    noengine_witness(monkeypatch)


def test_mutant_noengine_admission_skip_is_caught(monkeypatch):
    with pytest.raises(AssertionError, match="NOENGINE"):
        noengine_witness(monkeypatch, mutant=True)


def test_ast_binary_collision_has_exact_witness(monkeypatch):
    from_ast, to_ast = UOp.sink(), UOp.sink(UOp.const(42, dtypes.int))
    # A fake KTPU AST exercises refusal before lowering or program construction.
    fake = SimpleNamespace(key=from_ast.key)
    monkeypatch.setitem(seam.PLANS, from_ast.key, object())
    monkeypatch.setitem(seam.PLAN_ASTS, from_ast.key, to_ast)
    renderer = SimpleNamespace(target=SimpleNamespace(device="KTPU"))
    with pytest.raises(seam.KTPUUnsupported, match="collision"):
        seam.to_program(fake, renderer)
