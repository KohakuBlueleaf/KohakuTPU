"""Small v9t6s value vectors; run with one model worker and a 300s alarm."""

import json
import time

import numpy as np

from kohakutpu.application.runtime import packages as B
from kohakutpu.compiler import _rs
from kohakutpu.stack import rs

SOURCE = """target ktpu.v9
image copy(x, y)
    desc a0 = x walk=(32, 8)
    desc a1 = y walk=(32, 8)
    vfill a0 -> l1[0]
    vbar
    vunpk v0 <- l1[0] f16
    vpack v0 -> l1[16] f16
    vsync drain on=pack
    vdrain a1 <- l1[16]
    halt
fn copy.l1(x: f16[128] @dram, y: f16[128] @dram)
    send vc[0]
        run copy(x, y)
    barrier
"""


def prebuilt(machine, x, y, source=SOURCE):
    out = _rs.compile(
        source,
        machine,
        "copy",
        l1=True,
        fetch=True,
        binds={"x": x, "y": y},
        cache_dir="",
    )
    p = out["packages"][0]
    at = {"x": x, "y": y}
    bindings = tuple(at[q] if q in at else int(q[1:], 16) for q in p["params"])
    return B.Prebuilt(
        p["bytes"],
        bindings,
        machine,
        0,
        (),
        False,
        rs.fetch(machine),
        images_json=p["images_json"],
    )


def imem_count(p, cores=None):
    raw = p.bound(False, cores=None if cores is None else cores.snapshot())
    tables = _rs.package_read(raw, p.machine_json)
    shift = json.loads(p.machine_json)["machine"]["die"]["noc"]["data_bits"] - 4
    return sum(
        word >> shift == 1
        for op, _, _, count, arg in tables["steps"]
        if op == 1
        for word in tables["payloads"][arg : arg + count]
    )


def test_same_kernel_twice_and_rebound(dev, monkeypatch):
    assert dev.kind == "sim"
    card = dev.card
    card.alarm = 300
    x, y, other = [card.alloc(256) for _ in range(3)]
    values = np.arange(128, dtype=np.float16)
    changed = -values
    card.write(x, values.tobytes())
    card.write(other, changed.tobytes())
    p = prebuilt(card.machine_json, x, y)
    rebound = prebuilt(card.machine_json, other, y)
    counts, results = [], []
    start = time.perf_counter()
    try:
        for resident in (False, True):
            monkeypatch.setenv("KOHAKUTPU_RESIDENT", "1" if resident else "0")
            card.residents[0].forget()
            for _ in range(2):
                card.write(y, bytes(256))
                counts.append(imem_count(p, card.residents[0] if resident else None))
                result = card.run(p)
                results.append(result["cycles"])
                assert card.get(y, 256) == values.tobytes()
        assert counts[0] == counts[1] == counts[2] > 0
        assert counts[3] == 0
        card.run(rebound)
        assert card.get(y, 256) == changed.tobytes()
        card.record()
        card.run(p)
        log = card.recorded()
        card.run(rebound)
        card.replay(log)
        assert card.get(y, 256) == values.tobytes()
        assert imem_count(p, card.residents[0]) > 0
        card.run(rebound)
        assert card.get(y, 256) == changed.tobytes()
        print(
            json.dumps(
                {
                    "imem_words": counts,
                    "cycles": results,
                    "wall_seconds": time.perf_counter() - start,
                }
            )
        )
    finally:
        for addr in (x, y, other):
            card.free(addr)


def test_register_counts_and_lowered_engine_values(dev, monkeypatch):
    card = dev.card
    card.alarm = 300
    monkeypatch.setenv("KOHAKUTPU_RESIDENT", "1")
    x, y = card.alloc(256), card.alloc(256)
    values = np.arange(128, dtype=np.float16)
    p = prebuilt(card.machine_json, x, y)
    table = _rs.package_read(p.package, p.machine_json)
    i = next(i for i, s in enumerate(table["steps"]) if s[0] == 1)
    n = table["steps"][i][3]
    meta = json.dumps(
        {
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
            "images": json.loads(p.images_json),
        }
    )
    register = _rs.bind(
        p.package,
        list(p.bindings),
        p.machine_json,
        params={"n": n},
        template_json=meta,
        count_path="register",
    )
    engine = p.bound(True)
    try:
        card.write(x, values.tobytes())
        for raw in (register, engine):
            q = B.Prebuilt(raw, (), p.machine_json, 0, (), False, p.fetch)
            card.residents[0].forget()
            assert imem_count(q, card.residents[0]) > 0
            card.run(q)
            assert card.get(y, 256) == values.tobytes()
            assert imem_count(q, card.residents[0]) == 0
            card.write(y, bytes(256))
            card.run(q)
            assert card.get(y, 256) == values.tobytes()
    finally:
        card.free(x)
        card.free(y)


def test_bound_instruction_bytes_change_values(dev, monkeypatch):
    card = dev.card
    card.alarm = 300
    monkeypatch.setenv("KOHAKUTPU_RESIDENT", "1")
    x, y = card.alloc(256), card.alloc(256)
    values = np.arange(128, dtype=np.float16)
    source = SOURCE.replace(
        "    vpack v0", "    seti s0 = 1.0\n    vmul v0, v0, s0\n    vpack v0"
    )
    p = prebuilt(card.machine_json, x, y, source)
    other = prebuilt(card.machine_json, x, y, source.replace("s0 = 1.0", "s0 = 2.0"))
    a = _rs.package_read(p.package, p.machine_json)["payloads"]
    b = _rs.package_read(other.package, other.machine_json)["payloads"]
    index = next(i for i, (old, new) in enumerate(zip(a, b, strict=True)) if old != new)
    table = json.dumps(
        {
            "parameters": {"word": {"lo": 0, "hi": (1 << 32) - 1}},
            "sites": [
                {
                    "name": "bound-scale",
                    "payload": index,
                    "bit": 0,
                    "width": 32,
                    "coefficients": {"word": 1},
                }
            ],
        }
    )
    rebound = _rs.bind(
        p.package,
        list(p.bindings),
        p.machine_json,
        template_json=table,
        params={"word": b[index] & 0xFFFFFFFF},
    )
    q = B.Prebuilt(rebound, (), p.machine_json, 0, (), False, p.fetch)
    try:
        card.write(x, values.tobytes())
        card.residents[0].forget()
        card.run(p)
        assert card.get(y, 256) == values.tobytes()
        assert imem_count(q, card.residents[0]) > 0
        card.run(q)
        assert card.get(y, 256) == (values * np.float16(2)).tobytes()
    finally:
        card.free(x)
        card.free(y)
