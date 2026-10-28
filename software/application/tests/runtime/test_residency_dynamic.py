"""Compiler manifests and register-count/engine residency on the Rust API."""

import json

import pytest

from kohakuaccel.simulation.package import Interpreter
from kohakutpu.compiler import _rs
from kohakutpu.simulation.package import Reader
from kohakutpu.stack import rs

SOURCE = """target ktpu.v9
image image()
    seti s0 = 7
    halt
fn k.l1()
    send vc[0]
        run image()
    barrier
"""


def fixture():
    machine = rs.machine_json("vlt_v9t6s_1n")
    compiled = _rs.compile(SOURCE, machine, "k", l1=True, fetch=True, cache_dir="")
    return machine, compiled["packages"][0]


def count(raw, machine):
    table = _rs.package_read(raw, machine)
    return sum(
        w >> 252 == 1
        for op, _, _, n, at in table["steps"]
        if op == 1
        for w in table["payloads"][at : at + n]
    )


def test_compiler_manifest_and_t3_no_compile_hit(monkeypatch):
    machine, package = fixture()
    images = json.loads(package["images_json"])
    assert images["images"]
    table = _rs.package_read(package["bytes"], machine)
    i = next(i for i, s in enumerate(table["steps"]) if s[0] == 1)
    n = table["steps"][i][3]
    metadata = {
        "parameters": {"n": {"lo": n, "hi": n}},
        "sites": [
            {
                "name": "words",
                "step": i,
                "bit": 32,
                "width": 32,
                "coefficients": {"n": 1},
            }
        ],
        "images": images,
    }
    monkeypatch.setattr(_rs, "compile", lambda *a, **kw: pytest.fail("compiled on hit"))
    cores = _rs.Cores(machine)
    kwargs = dict(
        cores=cores,
        params={"n": n},
        template_json=json.dumps(metadata),
        count_path="register",
    )
    cold = _rs.bind(package["bytes"], [], machine, **kwargs)
    warm = _rs.bind(package["bytes"], [], machine, **kwargs)
    assert count(cold, machine) > 0
    assert count(warm, machine) == 0
    images["images"][0]["sites"] = [0]
    with pytest.raises(Exception, match="manifest"):
        _rs.bind(package["bytes"], [], machine, images_json=json.dumps(images))


def test_already_lowered_engine_uses_current_residency():
    machine, package = fixture()
    low = _rs.bind(package["bytes"], [], machine, lower=True)
    assert any(s[0] == 11 for s in _rs.package_read(low, machine)["steps"])
    cores = _rs.Cores(machine)
    cold = _rs.bind(low, [], machine, cores=cores)
    warm = _rs.bind(low, [], machine, cores=cores)
    assert count(cold, machine) > 0
    assert count(warm, machine) == 0


class InstructionWorld:
    def __init__(self):
        self.pending = []
        self.installs = 0
        self.words = {}
        self.runs = []

    def send(self, x, y, payload):
        code = payload >> 252
        if code == 1:
            addr = (payload >> 243) & 511
            self.words[addr] = payload & 0xFFFFFFFF
            self.installs += 1
        elif code == 3:
            self.runs.append(self.words[0])
        self.pending.append((x, y, 0, 0))

    def drain(self):
        pending, self.pending = self.pending, []
        return pending


@pytest.mark.parametrize("image_patch", [False, True])
def test_runtime_patch_stream_values_and_installs(image_patch):
    machine, _ = fixture()
    x, y = json.loads(machine)["machine"]["die"]["units"]["vc"][0]
    words = [
        (1 << 252) | 123,
        (1 << 252) | (1 << 243) | (31 << 27),
        (2 << 252),
        (3 << 252),
    ]
    patch = 0 if image_patch else 2
    calls = [
        ["unit", [x, y]],
        ["alu", 0, 0, 0, 456],
        ["patch", 0, patch, 0, 32],
        ["dispatch", 0, [hex(w) for w in words]],
        ["barrier"],
    ]
    raw = _rs.package_build(
        json.dumps({"types": [[x, y, "VC"]], "credit": 16}), json.dumps(calls), machine
    )
    cores = _rs.Cores(machine)
    world = InstructionWorld()
    interp = Interpreter(world, reader=Reader(machine))
    for expected in (2, 1 if image_patch else 0):
        rewritten = _rs.bind(raw, [], machine, cores=cores)
        before = world.installs
        assert interp.run(rewritten).status == 0
        assert world.installs - before == expected
        assert world.runs[-1] == (456 if image_patch else 123)
