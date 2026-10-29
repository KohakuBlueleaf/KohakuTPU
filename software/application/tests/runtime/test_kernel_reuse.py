"""A launch seen before reuses its `Compiled`: no library read, no compile,
the same bound programs; a library entry's reuse still counts as a hit."""

import importlib
import json
import time

import numpy as np
import pytest

from kohakutpu.application.kernels import attention as KA
from kohakutpu.application.kernels import convert as KC
from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import lanes as KL
from kohakutpu.application.kernels import matmul as KM
from kohakutpu.application.kernels import transpose as KT
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime.device import Device
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.language import dsl as kt
from kohakutpu.language.dsl import errors as DE
from kohakutpu.language.dsl import trace as DT
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs

DK = importlib.import_module("kohakutpu.language.dsl.kernel")


def test_same_library_name_keeps_source_params_separate(monkeypatch):
    dev = Device(kind="offline", board="multimesh_v9t6", library="unused")

    def output(source):
        dev.counters["library_hits"] += 1
        return {"name": "same_name"}

    monkeypatch.setattr(dev, "compile_graph", output)
    monkeypatch.setattr(
        D.generated, "from_output", lambda dev, graph, output, traced: graph
    )
    first = dev.kernel('{"params": ["a"]}')
    second = dev.kernel('{"params": ["b"]}')
    assert first is not second
    assert second["params"] == ["b"]
    assert dev.kernel('{"params": ["a"]}') is first
    dev.close()


def graph(dev, n=128):
    kernel, given = V.flat_kernel(X.op("add", X.Load(0), 1.0), ("f16",), n, "f16", 2)
    return json.dumps(kernel.trace(given, machine=K.dsl_machine(dev.machine)).graph)


def test_a_repeated_launch_reads_and_builds_nothing(tmp_path, monkeypatch):
    dev = Device(
        kind="offline", board="multimesh_v9t6", library=tmp_path, library_only=True
    )
    source = graph(dev)
    rs._rs.library_prebuild(str(tmp_path), source, rs.machine_json(dev.machine))
    first = dev.kernel(source)
    x, y = dev.alloc(256), dev.alloc(256)
    dev.run(first, {"in0": x, "out": y})
    programs = first.programs(
        {k: dev.mmu.resolve(v)[1] for k, v in {"in0": x, "out": y}.items()}
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("a repeated launch read the library or compiled")

    monkeypatch.setattr(rs._rs, "library_get", forbidden)
    monkeypatch.setattr(rs._rs, "compile", forbidden)
    again = dev.kernel(source)
    assert again is first
    dev.run(again, {"in0": x, "out": y})
    assert (
        again.programs(
            {k: dev.mmu.resolve(v)[1] for k, v in {"in0": x, "out": y}.items()}
        )
        is programs
    )
    assert dev.counters["library_hits"] == 2
    assert dev.counters["kernel_reuses"] == 1
    assert dev.counters["compile_calls"] == 0
    assert dev.counters["runs"] == 2


def test_a_mutant_does_not_reuse_the_plain_kernel(tmp_path):
    dev = Device(kind="offline", board="multimesh_v9t6")
    source = graph(dev)
    plain = dev.kernel(source)
    mutant = min(rs._rs.MUTANTS)
    token = rs._MUTANT.set(mutant)
    try:
        assert dev.kernel(source) is not plain
    finally:
        rs._MUTANT.reset(token)
    assert dev.kernel(source) is plain


@pytest.mark.parametrize("kept", [1, 2])
def test_the_cache_is_bounded(monkeypatch, kept):
    monkeypatch.setattr(D, "KERNELS_KEPT", kept)
    dev = Device(kind="offline", board="multimesh_v9t6")
    for n in (128, 256, 512):
        dev.kernel(graph(dev, n))
    assert len(dev.kernels) == kept


def test_trace_cache_handles_array_consts_mutants_and_bounds(monkeypatch):
    runs = [0]
    orig_trace_init = DT.Trace.__init__

    def count_trace(self, *args, **kwargs):
        runs[0] += 1
        orig_trace_init(self, *args, **kwargs)

    monkeypatch.setattr(DT.Trace, "__init__", count_trace)

    @kt.kernel
    def scale_add(x=kt.In("R", 32), y=kt.Out("R", 32), *, weights=(1.0,)):
        bias = float(np.asarray(weights).sum())
        for c in kt.par(1, on=kt.vc):
            v = kt.unpack(kt.load(x[c * 4 : c * 4 + 4, :]))
            kt.store(y[c * 4 : c * 4 + 4, :], v + bias)

    dev = Device(kind="offline", board="multimesh_v9t6")
    machine = K.dsl_machine(dev.machine)
    given = {"x": kt.TensorSpec.make((4, 32))}
    arr = np.array([1.5, 2.5], dtype=np.float32)
    t1 = scale_add.trace(given, machine=machine, weights=arr)
    t2 = scale_add.trace(given, machine=machine, weights=arr.copy())
    assert t1 is t2
    assert runs[0] == 1

    # Serialising graph_json once caches the string and never calls json.dumps again.
    s1 = t1.graph_json
    monkeypatch.setattr(
        DK.json,
        "dumps",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("re-serialised graph")),
    )
    assert t1.graph_json is s1
    monkeypatch.undo()
    monkeypatch.setattr(DT.Trace, "__init__", count_trace)

    # In-place mutation of the array content produces a distinct trace key.
    arr[0] = 10.0
    t3 = scale_add.trace(given, machine=machine, weights=arr)
    assert t3 is not t1
    assert runs[0] == 2

    # Mutant-aware: setting rs._MUTANT does not reuse the plain trace.
    mutant = min(rs._rs.MUTANTS)
    token = rs._MUTANT.set(mutant)
    try:
        t_mut = scale_add.trace(given, machine=machine, weights=arr)
        assert t_mut is not t3
        assert runs[0] == 3
    finally:
        rs._MUTANT.reset(token)
    assert scale_add.trace(given, machine=machine, weights=arr) is t3
    assert runs[0] == 3

    # Mutant witness: when the trace cache is disabled, repeated trace re-runs body.
    monkeypatch.setattr(DK, "TRACES_KEPT", 0)
    scale_add._cache.clear()
    DK._TRACES.clear()
    before = runs[0]
    scale_add.trace(given, machine=machine, weights=arr)
    scale_add.trace(given, machine=machine, weights=arr)
    assert runs[0] == before + 2

    # Boundedness witness: TRACES_KEPT bounds both per-kernel and shared caches.
    monkeypatch.setattr(DK, "TRACES_KEPT", 2)
    scale_add._cache.clear()
    DK._TRACES.clear()

    def body(x, y, *, weights=(1.0,)):
        for c in kt.par(1, on=kt.vc):
            v = kt.unpack(kt.load(x[c * 4 : c * 4 + 4, :]))
            kt.store(y[c * 4 : c * 4 + 4, :], v + float(weights[0]))

    ports = {"x": kt.In("R", 32), "y": kt.Out("R", 32)}
    built = kt.Kernel.build("scale_add_b", body, ports, {"weights": (1.0,)})
    for k in (1.0, 2.0, 3.0, 4.0):
        built.trace(given, machine=machine, weights=(k,))
    assert len(built._cache) == 2
    assert len(DK._TRACES) == 2


def test_kernel_builders_are_memoized_and_bounded(monkeypatch):
    walk = ((4, 32), (16, 1))
    pl1 = KL.plan(128, walk, 0, walk, 0, 128, False)
    pl2 = KL.plan(128, walk, 0, walk, 0, 128, False)
    assert pl1 is pl2
    assert KL.relayout_kernel(pl1, 2)[0] is KL.relayout_kernel(pl2, 2)[0]
    assert KT.transpose_kernel(1, 16, 16, 2)[0] is KT.transpose_kernel(1, 16, 16, 2)[0]
    assert (
        KT.reverse_kernel(64, ((2, -32),), 32, 32, 2)[0]
        is KT.reverse_kernel(64, ((2, -32),), 32, 32, 2)[0]
    )
    assert KC.copy_kernel(64)[0] is KC.copy_kernel(64)[0]
    assert (
        KC.walk_kernel(64, ((2, 32),), 0, 64, ((2, 32),), 0)[0]
        is KC.walk_kernel(64, ((2, 32),), 0, 64, ((2, 32),), 0)[0]
    )
    assert (
        KC.fill_kernel(64, ((2, 32),), 0, 0, 16)[0]
        is KC.fill_kernel(64, ((2, 32),), 0, 0, 16)[0]
    )
    op = V.Operand("full", pitch=32, rows=8)
    assert (
        KM.quant_kernel(op, 8, 32, 8, 32, "a", 2, 1)[0]
        is KM.quant_kernel(op, 8, 32, 8, 32, "a", 2, 1)[0]
    )
    mm = KM.plan(1, 8, 32, 32, machine_json=machine_of("vlt_v9t6s_1n").machine_json)
    expr = X.op("add", X.Load(0), 1.0)
    assert (
        KM.mm_kernel(mm, expr, (), op, 2, 2)[0]
        is KM.mm_kernel(mm, expr, (), op, 2, 2)[0]
    )
    assert (
        V.rows_kernel(expr, (op,), 8, 32, op, 2)[0]
        is V.rows_kernel(expr, (op,), 8, 32, op, 2)[0]
    )
    assert (
        V.flat_kernel(expr, ("f16",), 128, "f16", 2)[0]
        is V.flat_kernel(expr, ("f16",), 128, "f16", 2)[0]
    )
    assert KA.kernel(1, 64, 64, 64) is KA.kernel(1, 64, 64, 64)

    # DAG sharing distinction: shared vs unshared subexpression nodes do not collide.
    shared_node = X.op("add", X.Load(0), 1.0)
    dag_shared = X.op("mul", shared_node, shared_node)
    dag_tree = X.op("mul", X.op("add", X.Load(0), 1.0), X.op("add", X.Load(0), 1.0))
    assert (
        V.flat_kernel(dag_shared, ("f16",), 128, "f16", 2)[0]
        is not V.flat_kernel(dag_tree, ("f16",), 128, "f16", 2)[0]
    )

    # Mutant witness: setting KERNELS_KEPT=0 disables builder memoization.
    monkeypatch.setattr(KC, "KERNELS_KEPT", 0)
    KC._kernels.clear()
    assert KC.copy_kernel(64)[0] is not KC.copy_kernel(64)[0]

    # Boundedness witness: KERNELS_KEPT=2 bounds the builder cache.
    monkeypatch.setattr(KC, "KERNELS_KEPT", 2)
    KC._kernels.clear()
    for sz in (32, 64, 96, 128):
        KC.copy_kernel(sz)
    assert len(KC._kernels) == 2


def test_rust_bind_and_library_caches_with_mutants_and_bounds(tmp_path):
    dev = Device(
        kind="offline", board="multimesh_v9t6", library=tmp_path, library_only=True
    )
    machine = rs.machine_json(dev.machine)
    s1 = graph(dev, 128)
    s2 = graph(dev, 256)
    s3 = graph(dev, 512)
    for s in (s1, s2, s3):
        rs._rs.library_prebuild(str(tmp_path), s, machine)

    rs._rs.bind_cache_configure(
        machines=64, templates=1024, bound=4096, library=512, clear=True
    )
    try:
        # 1. Library cache hits in memory on repeat reads; mutant bypasses it.
        out1 = rs._rs.library_get(str(tmp_path), s1, machine)
        out2 = rs._rs.library_get(str(tmp_path), s1, machine)
        stats = rs._rs.bind_cache_stats()
        assert stats["library_reads"] == 1
        assert stats["library_hits"] == 1
        assert out1["name"] == out2["name"]

        rs._rs.library_get(str(tmp_path), s1, machine, "bindings.library/no_cache")
        stats_mut = rs._rs.bind_cache_stats()
        assert stats_mut["library_reads"] == 2
        assert stats_mut["library_hits"] == 1

        # 2. Bind cache reuses parsed machine, parsed template, and bound bytes;
        # outputs are byte-identical to the no-cache mutant.
        pkg = out1["packages"][0]["bytes"]
        img = out1["packages"][0]["images_json"]
        rs._rs.bind_cache_configure(clear=True)
        b1 = rs._rs.bind(pkg, [0x100000, 0x200000], machine, 0, True, images_json=img)
        b2 = rs._rs.bind(pkg, [0x100000, 0x200000], machine, 0, True, images_json=img)
        b_nocache = rs._rs.bind(
            pkg,
            [0x100000, 0x200000],
            machine,
            0,
            True,
            mutant="bindings.bind/no_cache",
            images_json=img,
        )
        assert b1 == b2 == b_nocache
        st = rs._rs.bind_cache_stats()
        assert st["machine_hits"] >= 1
        assert st["template_hits"] == 1
        assert st["bound_hits"] == 1

        # Different virtual handles translating to the same physical addresses hit bound cache.
        tr_a = [(0x1_0000_0000, 0x100000, 4096), (0x1_0000_1000, 0x200000, 4096)]
        tr_b = [(0x1_0000_2000, 0x100000, 4096), (0x1_0000_3000, 0x200000, 4096)]
        h1 = rs._rs.bind(
            pkg,
            [0x1_0000_0000, 0x1_0000_1000],
            machine,
            0,
            True,
            translations=tr_a,
            images_json=img,
        )
        hits_before = rs._rs.bind_cache_stats()["bound_hits"]
        h2 = rs._rs.bind(
            pkg,
            [0x1_0000_2000, 0x1_0000_3000],
            machine,
            0,
            True,
            translations=tr_b,
            images_json=img,
        )
        assert h1 == h2 == b1
        assert rs._rs.bind_cache_stats()["bound_hits"] == hits_before + 1

        # 3. Boundedness of Rust caches.
        rs._rs.bind_cache_configure(
            machines=1, templates=1, bound=2, library=2, clear=True
        )
        for s in (s1, s2, s3):
            got = rs._rs.library_get(str(tmp_path), s, machine)
            p = got["packages"][0]["bytes"]
            rs._rs.bind(p, [0x100000, 0x200000], machine, 0, False)
            rs._rs.bind(p, [0x100000, 0x200000], machine, 0, True)
        bounded = rs._rs.bind_cache_stats()
        assert bounded["machines_len"] <= 1
        assert bounded["templates_len"] <= 1
        assert bounded["bound_len"] <= 2
        assert bounded["library_len"] <= 2
    finally:
        rs._rs.bind_cache_configure(
            machines=64, templates=1024, bound=4096, library=512, clear=True
        )
        dev.close()


def test_at_caller_caches_basename(monkeypatch):
    DE._basename.cache_clear()
    calls = [0]
    orig_path = DE.pathlib.Path
    expected_prefix = orig_path(__file__).name + ":"

    def counting_path(*args, **kwargs):
        calls[0] += 1
        return orig_path(*args, **kwargs)

    # Prime `_inside` and `_basename` so subsequent calls never construct Path.
    DE.at_caller()
    calls[0] = 0
    monkeypatch.setattr(DE.pathlib, "Path", counting_path)
    for _ in range(50):
        where = DE.at_caller()
        assert where.startswith(expected_prefix)
    assert calls[0] == 0


def test_thirty_two_warm_steps_stay_flat_and_bounded(tmp_path, monkeypatch):
    dev = Device(
        kind="offline", board="multimesh_v9t6", library=tmp_path, library_only=True
    )
    D.attach(dev)
    try:
        V._kernels.clear()
        DK._TRACES.clear()
        trace_calls = [0]
        orig_trace = DT.Trace.__init__

        def count_trace(self, *args, **kwargs):
            trace_calls[0] += 1
            orig_trace(self, *args, **kwargs)

        monkeypatch.setattr(DT.Trace, "__init__", count_trace)
        for n in (128, 256):
            rs._rs.library_prebuild(
                str(tmp_path), graph(dev, n), rs.machine_json(dev.machine)
            )
        expr = X.op("add", X.Load(0), 1.0)
        step_times = []
        for step in range(34):
            t0 = time.perf_counter()
            for n in (128, 256):
                nbytes = n * 2
                a = dev.alloc(nbytes)
                b = dev.alloc(nbytes)
                k, given = V.flat_kernel(expr, ("f16",), n, "f16", 2)
                FM.launch(dev, k, given, {"in0": a, "out": b})
                dev.free(a)
                dev.free(b)
            dev.drain()
            step_times.append(time.perf_counter() - t0)
            if step == 1:
                live_1 = len(dev.mmu._live)
                checked_1 = len(dev.card.checked)
                traces_1 = trace_calls[0]

        # Zero re-traces after step 0, and bounded MMU / card.checked across >= 30 warm steps.
        assert trace_calls[0] == traces_1 == 2
        assert len(dev.mmu._live) == live_1 == 0
        assert len(dev.card.checked) == checked_1 == 2
        assert dev.counters["compile_calls"] == 0
        assert dev.counters["library_misses"] == 0
        early = np.median(step_times[2:10])
        late = np.median(step_times[26:34])
        assert late <= max(early * 2.0, early + 0.002)

        # Mutant witness: if card.verify keys by per-launch Prebuilt id, card.checked leaks every launch.
        monkeypatch.setattr(
            type(dev.card),
            "verify",
            lambda self, progs: self.checked.setdefault(
                tuple((n, id(p)) for n, p in progs.items()), (tuple(progs.values()), [])
            )[1],
        )
        before_checked = len(dev.card.checked)
        a = dev.alloc(256)
        b = dev.alloc(256)
        k, given = V.flat_kernel(expr, ("f16",), 128, "f16", 2)
        FM.launch(dev, k, given, {"in0": a, "out": b})
        dev.free(a)
        dev.free(b)
        assert len(dev.card.checked) > before_checked
    finally:
        dev.close()
