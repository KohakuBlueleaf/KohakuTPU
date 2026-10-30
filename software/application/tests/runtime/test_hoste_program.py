"""Native-program owner scope, cancellation ownership, and resident invalidation."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.ktpugrad.recipe import Recording
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import native_launch
from kohakutpu.application.runtime.card import NodeCard
from kohakutpu.application.runtime.native_launch import descriptor
from kohakutpu.compiler import _rs
from kohakutpu.stack import rs


def device(monkeypatch):
    monkeypatch.setenv("KTPU_NATIVE_MEMORY", "1")
    return D.Device("offline", board="multimesh_v9t6", queue_depth=1)


def scope(dev):
    return dev.recipe_epoch, dev.recipe_epoch.generation


def test_native_program_rejects_foreign_owner_recording_and_library_change(monkeypatch):
    a, b = device(monkeypatch), device(monkeypatch)
    try:
        program = _rs.LaunchProgram([], [], 0, scope(a))
        with pytest.raises(ValueError, match="invalidated"):
            program.launch(b, [])
        a.record()
        with pytest.raises(RuntimeError, match="non-recording"):
            program.launch(a, [])
        a.recorded().close()
        a.library = "new-library"
        with pytest.raises(ValueError, match="invalidated"):
            program.launch(a, [])
    finally:
        a.close()
        b.close()


def cancellation_witness(dev, factory):
    before = dev.dram.used
    program = factory(dev)
    with pytest.raises(MemoryError):
        program.launch(dev, [])
    assert dev.dram.used == before, "cancelled program leaked unadopted temporary"


def native_program(d):
    return _rs.LaunchProgram(
        [
            ("alloc", 0, 256, "dram", None, "temporary"),
            ("alloc", 1, d.dram.hi, "dram", None, "impossible"),
        ],
        [],
        0,
        scope(d),
    )


def test_native_program_cancellation_releases_unadopted_temporary(monkeypatch):
    dev = device(monkeypatch)
    try:
        cancellation_witness(dev, native_program)
    finally:
        dev.close()


def test_python_tape_cancellation_releases_unadopted_temporary(monkeypatch):
    dev = device(monkeypatch)
    try:
        tape = Recording(dev, {})
        tape.operations = [
            ("alloc", 0, 256, "dram", None, "temporary"),
            ("alloc", 1, dev.dram.hi, "dram", None, "impossible"),
        ]
        cancellation_witness(
            dev, lambda d: SimpleNamespace(launch=lambda d, b: tape.launch(d, {}))
        )
    finally:
        dev.close()


def test_mutant_native_program_cancellation_leaks_is_caught(monkeypatch):
    dev = device(monkeypatch)
    assert "bindings.program/leaks_cancelled" in rs.mutants()
    try:
        with (
            rs.mutant("bindings.program/leaks_cancelled"),
            pytest.raises(AssertionError, match="leaked"),
        ):
            cancellation_witness(dev, native_program)
    finally:
        dev.close()


def test_native_stateless_submission_forgets_previous_residency(monkeypatch):
    dev = device(monkeypatch)
    kernel, given = V.flat_kernel(X.op("add", X.Load(0), 1.0), ("f16",), 128, "f16", 2)
    compiled = dev.specialise(kernel, given)
    handles = [dev.alloc(4096) for _ in compiled.l1.params(compiled.name, "l1")]
    binds = dict(
        zip(
            (p for p, _ in compiled.l1.params(compiled.name, "l1")),
            handles,
            strict=True,
        )
    )
    captured = []

    def capture(compiled, full, progs, submitted):
        captured.append(descriptor(dev, compiled, full, progs, submitted, binds))

    try:
        dev.resident_mode = "0"
        with patch.object(dev, "_recipe_capture", capture):
            dev.run(compiled, binds)
        native = captured[0]
        dev.resident_mode = "1"
        dev.run(compiled, binds)
        cores = dev.card.residents[0]
        coords = dev.machine.mesh(0).units["VC"]
        assert any(cores.resident(c) for c in coords)
        dev.collect_through(native.launch(dev, handles))
        assert not any(cores.resident(c) for c in coords)
    finally:
        dev.close()


def test_a_run_lays_out_its_own_bytes_in_a_larger_window(monkeypatch):
    dev = device(monkeypatch)
    line = max(dev.line_bytes, 256)
    try:
        at = dev._window(("dram", None), 1 << 20)
        spans = [("a", line + 1, "dram", None, ()), ("b", 1, "dram", None, ())]
        windowed = dev._windowed(spans)
        assert windowed == {"a": at, "b": at + 2 * line}
        assert dev._extents(spans, windowed) == {at: 2 * line + 1}
    finally:
        dev.close()


def test_a_native_replay_carries_the_run_sizes_not_the_window(monkeypatch):
    """RUL.020: the replay's scratch is the run's own bytes, one block a
    window, with every name in it at its offset; a name the caller bound is
    the caller's handle."""
    dev = device(monkeypatch)
    window = SimpleNamespace(virtual=0x1000, extent=1 << 20, arena=object())
    resolved = SimpleNamespace(resolve=lambda address: (window, address))
    fake = SimpleNamespace(
        mmu=resolved,
        memory=dev.memory,
        resident_mode=dev.resident_mode,
        recipe_epoch=dev.recipe_epoch,
    )
    built = SimpleNamespace(NativeSubmit=lambda *fields: fields)
    monkeypatch.setattr(native_launch, "_rs", built)
    try:
        submitted = SimpleNamespace(kind="dsl", info={}, extents={0x1000: 768})
        fields = descriptor(
            fake,
            SimpleNamespace(name="k"),
            {"given": 0x9000, "a": 0x1000, "b": 0x1200},
            {},
            submitted,
            {"given": 0x9000},
        )
    finally:
        dev.close()
    assert fields[3] == [(768, "dram", None, "scratch:k")]
    assert fields[4] == [(0, 0, 0), (1, 0, 0), (1, 0, 0x200)]


def test_a_window_free_is_not_a_recorded_operation(monkeypatch):
    """A window outlives its run and belongs to the device, so a recording
    replays nothing for it; any other free is the recording's."""
    dev = device(monkeypatch)
    try:
        at = dev._window(("dram", None), 4096)
        tape = Recording(dev, {})
        temporary = dev.alloc(4096)
        tape.on_alloc(temporary, 4096, "dram", None, "temporary")
        before = list(tape.operations)
        tape.on_free(at)
        assert tape.operations == before and tape.unsupported is None
        tape.on_free(temporary)
        assert tape.operations[-1] == ("free", ("temp", 0, None, 0))
    finally:
        dev.close()


def test_submit_wire_single_nonfirst_node_and_mutant():
    card = object.__new__(NodeCard)
    card.nodes = (0, 1)
    card.debug = False
    card.log = None
    card.timeout = 1.0
    card.runs = 0
    card.machine_json = "{}"
    card._order, card._stdout, card._segs = {}, {}, {}
    card.residents = {}
    q0 = SimpleNamespace(
        flush=lambda: None,
        wait=lambda tag, timeout=1.0: SimpleNamespace(cycles=10, record=None),
        read_stdout=lambda: "",
        record=None,
    )
    q1 = SimpleNamespace(
        flush=lambda: None,
        wait=lambda tag, timeout=1.0: SimpleNamespace(cycles=42, record=None),
        read_stdout=lambda: "",
        record=None,
    )
    card.queues = {0: q0, 1: q1}
    card.q = q0
    card._submit = lambda n, data, bindings, admitted=False: 7
    recipe = SimpleNamespace(plain=lambda h, t: b"plain")

    def single_node_witness():
        pending = card.submit_wire({1: (b"wire", ())}, {1: (recipe, [0], [])})
        got = card.collect(pending)
        assert got["cycles"] == 42 and got["nodes"][1]["cycles"] == 42

    single_node_witness()
    assert "app.card/single_by_count" in rs.mutants()
    with rs.mutant("app.card/single_by_count"), pytest.raises(KeyError):
        single_node_witness()
