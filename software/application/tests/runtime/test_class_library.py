"""Production dispatch, exact-source caching, and mis-admission refusal."""

import json

import pytest

from kohakutpu.application.kernels import convert as KC
from kohakutpu.application.runtime import generated
from kohakutpu.application.runtime.class_library import ClassLibrary
from kohakutpu.application.runtime.device import Device
from kohakutpu.application.runtime.packages import Prebuilt
from kohakutpu.application.runtime.template_store import TemplateStore
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def fixture(dev, root):
    machine = rs.machine_json(dev.machine)
    source = rs._rs.generate(
        "vector.flat_kernel",
        json.dumps(
            {
                "e": {"nodes": [{"load": 0}], "root": 0},
                "dtypes": ["f16"],
                "n": 128,
                "out_dtype": "f16",
            }
        ),
        machine,
    )
    rs._rs.library_prebuild(str(root), source, machine)
    output = rs._rs.library_get(str(root), source, machine)
    table = json.dumps({"parameters": {"n": {"lo": 128, "hi": 128}}, "sites": []})
    refs, bindings = {}, {}
    for mesh, package in output["packages"].items():
        buffers = rs._rs.package_read(package["bytes"], machine, mesh)["buffers"]
        bindings[mesh] = [0x10000000 + i * 0x1000000 for i in range(len(buffers))]
        TemplateStore(root, machine).admit(
            source, package["bytes"], table, bindings[mesh]
        )
        refs[mesh] = {"source": source, "table": table, "params": {"n": 128}}
    ClassLibrary(root, machine).admit(source, output, refs, bindings)
    return source


def copy_pair_fixture(dev, root):
    """Two distinct copy launches (64B and 192B) sharing one certified class template."""
    machine = rs.machine_json(dev.machine)
    dsl_m = K.dsl_machine(dev.machine)
    sources, outputs = {}, {}
    for words in (2, 6):
        k, given = KC.copy_kernel(words * 32)
        src = k.trace(given, machine=dsl_m).graph_json
        rs._rs.library_prebuild(str(root), src, machine)
        sources[words] = src
        outputs[words] = rs._rs.library_get(str(root), src, machine)
    derived = rs._rs.template_derive(
        [
            outputs[2]["packages"][0]["bytes"],
            outputs[6]["packages"][0]["bytes"],
        ],
        json.dumps([{"i": 0}, {"i": 1}]),
        json.dumps({"i": {"lo": 0, "hi": 1}}),
        machine,
        values_json=json.dumps({"w": {"op": "param", "name": "i"}}),
    )
    raw = derived["bytes"]
    table_obj = json.loads(derived["template_json"])
    table_obj["parameters"] = {"words": {"lo": 2, "hi": 6}}
    for site in table_obj["sites"]:
        coef = site["coefficients"].get("i", site["coefficients"].get("w", 0)) // 4
        site["constant"] = site["constant"] - 2 * coef
        site["coefficients"] = {"words": coef}
    table = json.dumps(table_obj)
    buffers = rs._rs.package_read(raw, machine)["buffers"]
    bindings = [0x10000000 + i * 0x1000000 for i in range(len(buffers))]
    TemplateStore(root, machine).admit(sources[2], raw, table, bindings)
    clib = ClassLibrary(root, machine)
    for words in (2, 6):
        ref = {0: {"source": sources[2], "table": table, "params": {"words": words}}}
        clib.admit(sources[words], outputs[words], ref, {0: bindings})
    return sources, outputs, table, bindings


@pytest.mark.parametrize("classes", [False, True])
def test_production_class_dispatch_and_bind(tmp_path, monkeypatch, classes):
    dev = Device(
        kind="offline",
        board="multimesh_v9t6",
        library=tmp_path,
        library_only=True,
        classes=classes,
    )
    try:
        source = fixture(dev, tmp_path)

        def forbidden(*args, **kwargs):
            raise AssertionError("compile called after prebuild")

        monkeypatch.setattr(rs._rs, "compile", forbidden)
        if classes:
            monkeypatch.setattr(rs._rs, "library_get", forbidden)
        output = dev.compile_graph(source)
        compiled = generated.from_output(dev, json.loads(source), output)
        x, y = dev.alloc(256), dev.alloc(256)
        dev.run(compiled, {"in0": x, "out": y})
        assert dev.counters["class_hits"] == int(classes)
        assert dev.counters["exact_hits"] == int(not classes)
        assert dev.counters["library_misses"] == 0
        assert dev.counters["compile_calls"] == 0
        assert dev.counters["runs"] == 1
    finally:
        dev.close()


def test_class_sharing_and_eager_exact_source_caches(tmp_path, monkeypatch):
    dev = Device(
        kind="offline",
        board="multimesh_v9t6",
        library=tmp_path,
        library_only=True,
        classes=True,
    )
    try:
        sources, outputs, _, _ = copy_pair_fixture(dev, tmp_path)
        machine = rs.machine_json(dev.machine)
        rs._rs.bind_cache_configure(clear=True)

        # Both launches hit the class library, cache per exact source, and bind
        # with their own buffer sizes (64B for words=2, even though class max is 192B).
        c2 = dev.kernel(sources[2])
        c6 = dev.kernel(sources[6])
        assert c2 is not c6
        assert dev.counters["class_hits"] == 2
        assert dev.counters["exact_hits"] == 0
        assert dev.counters["library_hits"] == 2

        a2, b2 = dev.alloc(64), dev.alloc(64)
        a6, b6 = dev.alloc(192), dev.alloc(192)
        dev.run(c2, {"src": a2, "dst": b2})
        dev.run(c6, {"src": a6, "dst": b6})
        # A launch binds once; the next launch of a source binds through the cache.
        dev.run(c2, {"src": a2, "dst": b2})
        stats = rs._rs.bind_cache_stats()
        assert stats["template_hits"] >= 1

        # Bound packages from class lookup are byte-identical to exact packages.
        p2_class = c2.programs({"src": 0x100000, "dst": 0x200000})[None].bound(False)
        p6_class = c6.programs({"src": 0x100000, "dst": 0x200000})[None].bound(False)
        p2_exact = rs._rs.bind(
            outputs[2]["packages"][0]["bytes"], [0x100000, 0x200000], machine, 0, False
        )
        p6_exact = rs._rs.bind(
            outputs[6]["packages"][0]["bytes"], [0x100000, 0x200000], machine, 0, False
        )
        assert p2_class == p2_exact
        assert p6_class == p6_exact
        assert p2_class != p6_class

        # Repeated exact source reuses Compiled without touching ClassLibrary or disk,
        # while still incrementing class_hits and library_hits.
        def forbidden(*args, **kwargs):
            raise AssertionError("repeated launch touched ClassLibrary or compiler")

        monkeypatch.setattr(ClassLibrary, "lookup", forbidden)
        monkeypatch.setattr(rs._rs, "library_get", forbidden)
        assert dev.kernel(sources[2]) is c2
        assert dev.kernel(sources[6]) is c6
        assert dev.counters["kernel_reuses"] == 2
        assert dev.counters["class_hits"] == 4
        assert dev.counters["library_hits"] == 4
    finally:
        dev.close()


def test_misadmitted_member_never_returns_differing_package(tmp_path):
    dev = Device(
        kind="offline",
        board="multimesh_v9t6",
        library=tmp_path,
        library_only=True,
        classes=True,
    )
    try:
        sources, outputs, table, bindings = copy_pair_fixture(dev, tmp_path)
        machine = rs.machine_json(dev.machine)
        clib = ClassLibrary(tmp_path, machine)

        # 1. Admission refuses wrong parameters (words=2 for the words=6 launch).
        bad_ref = {0: {"source": sources[2], "table": table, "params": {"words": 2}}}
        with pytest.raises(ValueError, match="first difference"):
            clib.admit(sources[6], outputs[6], bad_ref, {0: bindings})

        # 2. Admission refuses mismatched output metadata (outputs[2] for sources[6]).
        with pytest.raises(ValueError, match="does not match launch"):
            clib.admit(sources[6], outputs[2], bad_ref, {0: bindings})

        # 3. Deliberately mis-admit words=6 with words=2 parameters (via mutant or disk tamper).
        with rs.mutant("l0.template/misadmit"):
            clib.admit(sources[6], outputs[6], bad_ref, {0: bindings})

        # Normal class-first lookup refuses to return a package differing from exact.
        with pytest.raises(ValueError, match="differs"):
            dev.compile_graph(sources[6])

        # Mutant witness: bypassing the lookup equivalence guard returns the wrong package.
        bound2 = rs._rs.bind(
            outputs[2]["packages"][0]["bytes"], bindings, machine, 0, False
        )
        with rs.mutant("l0.template/misadmit"):
            mis_out = dev.compile_graph(sources[6])
            mis_compiled = generated.from_output(dev, json.loads(sources[6]), mis_out)
            mis_bound = mis_compiled.programs({"src": bindings[0], "dst": bindings[1]})[
                None
            ].bound(False)
            assert mis_bound == bound2
    finally:
        dev.close()


def test_class_launch_outside_domain_refuses_at_production_bind(tmp_path):
    dev = Device(
        kind="offline", board="multimesh_v9t6", library=tmp_path, library_only=True
    )
    try:
        source = fixture(dev, tmp_path)
        output = dev.compile_graph(source)
        output["packages"][0]["template_params"]["n"] = 129
        compiled = generated.from_output(dev, json.loads(source), output)
        x, y = dev.alloc(256), dev.alloc(256)
        with pytest.raises(ValueError, match="outside"):
            dev.run(compiled, {"in0": x, "out": y})
        assert dev.counters["compile_calls"] == 0
    finally:
        dev.close()


def test_store_rejects_changed_physical_aliases(tmp_path):
    dev = Device(kind="offline", board="multimesh_v9t6")
    try:
        source = fixture(dev, tmp_path)
        machine = rs.machine_json(dev.machine)
        output = ClassLibrary(tmp_path, machine).lookup(source)
        package = output["packages"][0]
        table = package["template_json"]
        buffers = rs._rs.package_read(package["bytes"], machine)["buffers"]
        assert len(buffers) == 2
        handles = [0x1000, 0x2000]
        # Distinct handles and valid extents, but both resolve to the same DRAM bytes.
        aliases = [(h, 0x10000000, b["size"]) for h, b in zip(handles, buffers)]
        kwargs = dict(params={"n": 128}, template_json=table, translations=aliases)
        with pytest.raises(ValueError, match="aliasing/overlap"):
            rs._rs.bind(package["bytes"], handles, machine, **kwargs)
        # Red witness: deleting only the environment guard admits the bad bind.
        assert rs._rs.bind(
            package["bytes"],
            handles,
            machine,
            mutant="l0.template/environment",
            **kwargs,
        )
        translated = [
            (h, 0x10000000 + i * 0x1000000, b["size"])
            for i, (h, b) in enumerate(zip(handles, buffers))
        ]
        kwargs["translations"] = translated
        assert rs._rs.bind(package["bytes"], handles, machine, **kwargs)
    finally:
        dev.close()


@pytest.mark.parametrize("adam", [False, True])
def test_submit_selects_actual_environment(tmp_path, monkeypatch, adam):
    dev = Device(
        kind="offline", board="multimesh_v9t6", library=tmp_path, library_only=True
    )
    try:
        source = fixture(dev, tmp_path)
        machine = rs.machine_json(dev.machine)
        if adam:
            source = rs._rs.generate(
                "vector.flat_kernel",
                json.dumps(
                    {
                        "e": {
                            "nodes": [
                                {"load": 0},
                                {"const": 0.9},
                                {"op": "mul", "args": [0, 1]},
                                {"load": 1},
                                {"const": 0.1},
                                {"op": "mul", "args": [3, 4]},
                                {"op": "add", "args": [2, 5]},
                            ],
                            "root": 6,
                        },
                        "dtypes": ["f16", "f16"],
                        "n": 10,
                    }
                ),
                machine,
            )
            rs._rs.library_prebuild(str(tmp_path), source, machine)
            output = rs._rs.library_get(str(tmp_path), source, machine)
            raw = output["packages"][0]["bytes"]
            buffers = rs._rs.package_read(raw, machine)["buffers"]
            addresses = [0x10000000 + i * 0x1000000 for i in range(len(buffers))]
            table = json.dumps({"parameters": {}, "sites": []})
            TemplateStore(tmp_path, machine).admit(source, raw, table, addresses)
            ClassLibrary(tmp_path, machine).admit(
                source,
                output,
                {0: {"source": source, "table": table, "params": {}}},
                {0: addresses},
            )
        exact = rs._rs.library_get(str(tmp_path), source, machine)

        def forbidden(*args, **kwargs):
            raise AssertionError("environment selection invoked a compiler")

        monkeypatch.setattr(rs._rs, "compile", forbidden)
        monkeypatch.setattr(rs._rs, "compile_many", forbidden)
        seen = []
        original = dev.card.run

        def record(program):
            seen.append(program)
            return original(program)

        monkeypatch.setattr(dev.card, "run", record)
        a, b = dev.alloc(256), dev.alloc(256)
        inputs = {"in0": a}
        if adam:
            inputs["in1"] = dev.alloc(256)
        for out in (b, a, b, a):
            compiled = dev.kernel(source)
            dev.run(compiled, dict(inputs, out=out))
            p = seen[-1]
            addresses = [
                pa + h - va
                for h in p.bindings
                for va, pa, size in p.translations
                if va <= h < va + size
            ]
            assert p.bound(False) == rs._rs.bind(
                exact["packages"][0]["bytes"], addresses, machine
            )
            assert (p.template_json is None) == (out == a)
        assert dev.counters["compile_calls"] == 0
        assert dev.counters["library_misses"] == 0
        assert dev.counters["class_hits"] == dev.counters["exact_hits"] == 2
        # Selection mutant: ignore the actual environment, retaining the binder guard.
        monkeypatch.setattr(Prebuilt, "matches_environment", lambda self, mmu: True)
        with pytest.raises(ValueError, match="L0.BIND.044"):
            dev.run(dev.kernel(source), dict(inputs, out=a))
    finally:
        dev.close()


@pytest.mark.parametrize("reopen", [False, True])
def test_environment_identities_coexist(tmp_path, monkeypatch, reopen):
    dev = Device(
        kind="offline", board="multimesh_v9t6", library=tmp_path, library_only=True
    )
    try:
        source = fixture(dev, tmp_path)
        machine = rs.machine_json(dev.machine)
        output = rs._rs.library_get(str(tmp_path), source, machine)
        raw = output["packages"][0]["bytes"]
        table = json.dumps({"parameters": {"n": {"lo": 128, "hi": 128}}, "sites": []})
        store = TemplateStore(tmp_path, machine)
        separate, aliased = [0x10000000, 0x11000000], [0x10000000, 0x10000000]
        first = store.admit(source, raw, table, separate)
        if reopen:
            store = TemplateStore(tmp_path, machine)
        second = store.admit(source, raw, table, aliased)
        assert first != second
        assert store.admit(source, raw, table, separate) == first
        assert store.load_id(first)[1] != store.load_id(second)[1]
        refs = {0: {"source": source, "table": table, "params": {"n": 128}}}
        cached = dev.kernel(source)
        # Keep an old compiled candidate while a later prebuild changes the primary.
        ClassLibrary(tmp_path, machine).admit(source, output, refs, {0: separate})
        ClassLibrary(tmp_path, machine).admit(source, output, refs, {0: aliased})
        dev.counters["class_hits"] = 0
        dev.counters["library_hits"] = 0
        assert cached is dev.kernels[next(iter(dev.kernels))][0]
        seen = []
        original = dev.card.run

        def record(program):
            seen.append(program)
            return original(program)

        def forbidden(*args, **kwargs):
            raise AssertionError(
                "compiled or used exact fallback despite matching class"
            )

        monkeypatch.setattr(dev.card, "run", record)
        monkeypatch.setattr(rs._rs, "compile", forbidden)
        monkeypatch.setattr(rs._rs, "library_get", forbidden)
        a, b = dev.alloc(256), dev.alloc(256)
        for out in (a, b, a, b):
            dev.run(dev.kernel(source), {"in0": a, "out": out})
            p = seen[-1]
            assert p.template_json is not None
            expected_env = [[0, 1, 0]] if out == a else []
            assert json.loads(p.template_json)["environment"] == expected_env
            addresses = [dev.mmu.resolve(h)[1] for h in p.bindings]
            assert p.bound(False) == rs._rs.bind(raw, addresses, machine)
        assert dev.counters["class_hits"] == 4
        assert dev.counters["exact_hits"] == 0
        # Identity mutant: erase the environment before constructing the store key.
        identity = TemplateStore.identity
        monkeypatch.setattr(
            TemplateStore,
            "identity",
            lambda self, src, tab, environment=None: identity(self, src, tab, []),
        )
        with pytest.raises(ValueError, match="different translation environment"):
            store.admit(source, raw, table, aliased)
    finally:
        dev.close()


def test_class_env_and_api_override(monkeypatch):
    monkeypatch.setenv("KOHAKUTPU_LIBRARY_CLASSES", "0")
    for explicit, expected in [(None, False), (True, True)]:
        dev = Device(kind="offline", board="multimesh_v9t6", classes=explicit)
        try:
            assert dev.classes is expected
        finally:
            dev.close()
    monkeypatch.setenv("KOHAKUTPU_LIBRARY_CLASSES", "invalid")
    with pytest.raises(ValueError, match="must be 0 or 1"):
        Device(kind="offline", board="multimesh_v9t6")
