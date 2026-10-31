"""A capture holds the launches a run makes: same kernels, same counts, nothing compiled."""

import json
from collections import Counter
from dataclasses import asdict

import capture as C
import prebuild
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import generated
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import rs

BOARD = "multimesh_v9t6"


def classes(dev, monkeypatch):
    """The library class of each launch `dev` runs, in order, once run."""
    machine = rs.machine_json(dev.machine)
    sources = {}
    run = D.Device.run

    def observe(device, compiled, *a, **kw):
        sources.setdefault(compiled.name, compiled.traced.graph_json)
        return run(device, compiled, *a, **kw)

    monkeypatch.setattr(D.Device, "run", observe)
    return lambda: [
        json.loads(rs.library_key(sources[r.name], machine))["class"] for r in dev.runs
    ]


def test_capture_counts_every_launch_of_a_step(tmp_path):
    example = C.program()
    path = tmp_path / "capture.json"
    C.capture(path, "MLP")
    doc = json.loads(path.read_text(encoding="utf-8"))
    got = Counter(doc["records"][x["record"]]["name"] for x in doc["launches"])
    dev = D.attach(D.Device(kind="offline", board=BOARD))
    C.step(example, "MLP")
    assert got == Counter(r.name for r in dev.runs)
    assert set(C.FAMILIES.values()) & {r["generator"] for r in doc["records"]}


def test_prebuild_capture_holds_every_class_of_a_run(monkeypatch):
    doc = prebuild.capture(
        D.attach(D.Device(kind="offline", board=BOARD)), 8, True, "mlp"
    )
    dev = D.attach(D.Device(kind="offline", board=BOARD))
    keys = classes(dev, monkeypatch)
    prebuild.MODELS["mlp"](8, True, 1)
    assert keys() and set(keys()) <= set(doc["sources"])


def test_a_capture_takes_the_knobs_a_launch_takes(monkeypatch):
    """A launch's source is `generated.source` under the process's knobs, so a
    knob a run sets moves the class the capture records."""
    dev = D.attach(D.Device(kind="offline", board=BOARD))
    machine = machine_of(BOARD, packing_backend="vector_mx7")
    monkeypatch.setattr(dev, "machine", machine)
    src = Operand("full", pitch=64, offset=16, rows=5)
    args = {"src": asdict(src), "rows": 5, "cols": 48}
    args |= {"rp": 8, "cp": 64, "side": "a", "g": 2, "nk": 2}

    def key():
        source = generated.source(dev, "matmul.quant_kernel", args)
        return json.loads(rs.library_key(source, rs.machine_json(machine)))["class"]

    def probe(*a, **kw):
        binds = {"x": dev.alloc(4096), "q": dev.alloc(512)}
        generated.launch(dev, "matmul.quant_kernel", args, binds)

    monkeypatch.setitem(prebuild.MODELS, "probe", probe)
    plain = key()
    monkeypatch.setenv("KTPU_QUANT_PIPELINE", "0")
    assert key() != plain
    assert key() in prebuild.capture(dev, 8, True, "probe")["sources"]
