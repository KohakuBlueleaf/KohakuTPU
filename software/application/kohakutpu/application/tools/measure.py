"""Measure every example on a Verilated card in one run: each hand-written L1
example and each L2 example compiled to L1, checked against numpy, timed warm
(the second of two identical runs), with the efficiency its work has.

    python -m kohakutpu.application.tools.measure --build MODEL --nodes 0,1 --out DIR

Writes ``DIR/report.json`` (a row a case) and ``DIR/report.md`` (the table).
Efficiency: a matmul's MFU (multiply-adds over 1024 a cluster-cycle on every
cluster the card has), a vector kernel's lane use (lane beats over 16 a
core-cycle), a memory-bound kernel's bytes a cycle.
"""

import argparse
import json
import pathlib
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np

from kohakutpu.application.tools.l1 import run
from kohakutpu.compiler import target as T
from kohakutpu.compiler.l2 import pipeline
from kohakutpu.compiler.layout import tensor
from kohakutpu.language import examples
from kohakutpu.language.numerics.mxfp7 import value_fp16
from kohakutpu.simulation.verilator.card import VerilatorCard


def rel(got, want) -> float:
    return float(np.abs(got - want).max() / np.abs(want).max())


def silu(x):
    v = np.asarray(x, np.float64)
    return v / (1 + np.exp(-v))


def mxmm(x, w):
    return value_fp16(x) @ value_fp16(w).T


def matmul_case(m, n, k, seed=8, split=None):
    def make(rng):
        x = rng.standard_normal((m, k)).astype(np.float16)
        w = (rng.standard_normal((n, k)) / np.sqrt(k)).astype(np.float16)
        ins = {"x": x, "w": w}
        if split:
            ins = {"x": x} | {
                f"w{i}": w[i * split : (i + 1) * split] for i in range(n // split)
            }
        return ins, {"x": x, "w": w}

    def check(ref, out):
        return rel(out["y"], mxmm(ref["x"], ref["w"]))

    return make, check, ("mac", m * n * k)


def silu_case(rows):
    def make(rng):
        x = rng.standard_normal((rows, 128)).astype(np.float16)
        return {"x": x}, {"x": x}

    return (
        make,
        lambda r, o: rel(o["y"].reshape(r["x"].shape), silu(r["x"])),
        ("lane", None),
    )


def add_case():
    def make(rng):
        x, y = (rng.standard_normal((512, 128)).astype(np.float16) for _ in range(2))
        return {"x": x, "y": y}, {"x": x, "y": y}

    def check(r, o):
        return rel(o["out"].reshape(512, 128), r["x"].astype(np.float64) + r["y"])

    return make, check, ("bytes", 3 * 512 * 128 * 2)


def softmax_case():
    def make(rng):
        x = rng.standard_normal((1024, 128)).astype(np.float16)
        return {"x": x}, {"x": x}

    def check(r, o):
        v = r["x"].astype(np.float64)
        e = np.exp(v - v.max(1, keepdims=True))
        return rel(o["y"], e / e.sum(1, keepdims=True))

    return make, check, ("lane", None)


def layernorm_case():
    def make(rng):
        x = rng.standard_normal((1024, 128)).astype(np.float16)
        g = (rng.standard_normal(128) * 0.5 + 1).astype(np.float16)
        b = (rng.standard_normal(128) * 0.5).astype(np.float16)
        return {"x": x, "g": g, "b": b}, {"x": x, "g": g, "b": b}

    def check(r, o):
        v = r["x"].astype(np.float64)
        want = (v - v.mean(1, keepdims=True)) / np.sqrt(v.var(1, keepdims=True) + 1e-5)
        return rel(o["y"], want * r["g"] + r["b"])

    return make, check, ("lane", None)


def mlp_case():
    def make(rng):
        x = rng.standard_normal((256, 256)).astype(np.float16)
        w1, w2 = (
            (rng.standard_normal((256, 256)) / 16).astype(np.float16) for _ in range(2)
        )
        return {"x": x, "w1": w1, "w2": w2}, {"x": x, "w1": w1, "w2": w2}

    def check(r, o):
        h = mxmm(r["x"], r["w1"]).astype(np.float16)
        return rel(o["y"], mxmm(silu(h).astype(np.float16), r["w2"]))

    return make, check, ("mac", 2 * 256**3)


def mmsilu_case():
    make, _, _ = matmul_case(256, 256, 512, seed=30)
    return (
        make,
        lambda r, o: rel(o["y"], silu(mxmm(r["x"], r["w"]))),
        ("mac", 256 * 256 * 512),
    )


def tilesilu_case():
    def make(rng):
        x = rng.standard_normal((256, 256)).astype(np.float16)
        w = (rng.standard_normal((256, 256)) / 16).astype(np.float16)
        return {"x": x, "w": w}, {"x": x, "w": w}

    def check(r, o):
        return rel(o["y"], silu(mxmm(r["x"], r["w"]).astype(np.float16)))

    return make, check, ("mac", 256**3)


def mlpdirect_case():
    def make(rng):
        x = rng.standard_normal((64, 64)).astype(np.float16)
        w1 = (rng.standard_normal((128, 64)) / 8).astype(np.float16)
        w2 = (rng.standard_normal((64, 128)) / 11).astype(np.float16)
        return {"x": x, "w1": w1, "w2": w2}, {"x": x, "w1": w1, "w2": w2}

    def check(r, o):
        a = silu(mxmm(r["x"], r["w1"]).astype(np.float16)).astype(np.float16)
        return rel(o["y"], mxmm(a, r["w2"]))

    return make, check, ("mac", 2 * 64 * 64 * 128)


def mmsoftmax_case():
    def make(rng):
        x = rng.standard_normal((256, 128)).astype(np.float16)
        w = (rng.standard_normal((128, 128)) / 4).astype(np.float16)
        return {"x": x, "w": w}, {"x": x, "w": w}

    def check(r, o):
        s = mxmm(r["x"], r["w"]).astype(np.float16).astype(np.float64)
        p = np.exp(s - s.max(1, keepdims=True))
        return rel(o["y"], p / p.sum(1, keepdims=True))

    return make, check, ("mac", 256 * 128 * 128)


def convert_case():
    def make(rng):
        x = rng.standard_normal((64, 128)).astype(np.float16)
        return {"x": x}, {"x": x}

    def check(r, o):
        """0 when every output matches the host packing bit for bit, else 1."""
        x = r["x"]
        same = (
            bytes(o["a"]) == tensor.pack_a(x, 8, 2)
            and bytes(o["b"]) == tensor.pack_b(x, 16, 4)
            and np.array_equal(o["t"], tensor.fp16_values(x.view(np.uint16)))
        )
        return 0.0 if same else 1.0

    # Three reads of x, and a, b (MXFP7: half x's bytes) and t written.
    return make, check, ("bytes", 3 * 16384 + 2 * 8192 + 16384)


def leaky_case():
    def make(rng):
        x = rng.standard_normal((512, 128)).astype(np.float16)
        return {"x": x}, {"x": x}

    def check(r, o):
        v = r["x"].astype(np.float64)
        return rel(o["y"], np.where(v > 0, v, 0.1 * v))

    return make, check, ("lane", None)


def attention_case(heads, length=1024, d=64):
    """`heads` heads of base-2 attention, q and k at network-init magnitudes."""

    def make(rng):
        q = (rng.standard_normal((heads * length, d)) * 0.5).astype(np.float16)
        k = (rng.standard_normal((heads * length, d)) * 0.6).astype(np.float16)
        v = rng.standard_normal((heads * length, d)).astype(np.float16)
        return {"q": q, "k": k, "v": v}, {"q": q, "k": k, "v": v}

    def check(r, o):
        err, peak = 0.0, 0.0
        for h in range(heads):
            sl = slice(h * length, (h + 1) * length)
            s = value_fp16(r["q"][sl]) @ value_fp16(r["k"][sl]).T
            p = np.exp2(s - s.max(1, keepdims=True))
            want = (p / p.sum(1, keepdims=True)) @ r["v"][sl].astype(np.float64)
            err = max(err, float(np.abs(o["y"][sl] - want).max()))
            peak = max(peak, float(np.abs(want).max()))
        return err / peak

    return make, check, ("mac", 2 * heads * length * length * d)


def ffn_case(m=1024, d=1024, hidden=2048, glu=False):
    """silu(x w1^T) w2^T (with `glu`: times x w3^T before w2), network-init
    weights."""

    def make(rng):
        x = rng.standard_normal((m, d)).astype(np.float16)
        w1 = (rng.standard_normal((hidden, d)) / np.sqrt(d)).astype(np.float16)
        w2 = (rng.standard_normal((d, hidden)) / np.sqrt(hidden)).astype(np.float16)
        ins = {"x": x, "w1": w1, "w2": w2}
        if glu:
            ins["w3"] = (rng.standard_normal((hidden, d)) / np.sqrt(d)).astype(
                np.float16
            )
        return ins, ins

    def check(r, o):
        a = silu(mxmm(r["x"], r["w1"]).astype(np.float16))
        if glu:
            a = a * mxmm(r["x"], r["w3"]).astype(np.float16)
        return rel(o["y"], mxmm(a.astype(np.float16), r["w2"]))

    return make, check, ("mac", (3 if glu else 2) * m * d * hidden)


#: (example, levels, nodes, case): a case is (make inputs, check, work).
CASES = [
    ("matmul", ("l1", "l2"), 1, matmul_case(1024, 1024, 1024)),
    ("longk", ("l1", "l2"), 1, matmul_case(1024, 1024, 2048)),
    ("splitk", ("l1", "l2"), 1, matmul_case(256, 256, 1024)),
    ("mmsilu", ("l2",), 1, mmsilu_case()),
    ("mlp", ("l2",), 1, mlp_case()),
    ("ffn", ("l2",), 1, ffn_case()),
    ("swiglu", ("l2",), 1, ffn_case(glu=True)),
    ("tilesilu", ("l2",), 1, tilesilu_case()),
    ("mlpdirect", ("l2",), 1, mlpdirect_case()),
    ("mmsoftmax", ("l2",), 1, mmsoftmax_case()),
    ("convert", ("l2",), 1, convert_case()),
    ("silu", ("l1", "l2"), 1, silu_case(512)),
    ("add", ("l1", "l2"), 1, add_case()),
    ("softmax", ("l1", "l2"), 1, softmax_case()),
    ("lazysoftmax", ("l2",), 1, softmax_case()),
    ("leaky", ("l2",), 1, leaky_case()),
    ("layernorm", ("l1", "l2"), 1, layernorm_case()),
    ("flash", ("l1",), 1, attention_case(10)),
    ("flashd", ("l1",), 1, attention_case(10)),
    ("matmul2", ("l1", "l2"), 2, matmul_case(1024, 2048, 2048)),
    ("share2", ("l2",), 2, matmul_case(256, 2048, 512)),
    ("silu2", ("l1", "l2"), 2, silu_case(1024)),
    ("matmul4", ("l2",), 4, matmul_case(1024, 4096, 1024, split=1024)),
]


def compiled(name: str, nodes: int, tmp: pathlib.Path) -> str:
    """The L2 example through the whole pipeline (passes, lowering, image
    scheduling), as an L1 file."""
    m = examples.load(examples.example(name, "l2"))
    l1 = pipeline.compile_l1(m, name, T.machine(nodes=nodes))
    p = tmp / f"{name}.ktpu"
    p.write_text(l1.source, encoding="utf-8")
    return str(p)


def measure(build: str, nodes: tuple, name: str, level: str, out: str) -> dict:
    """One case at one level on a card of its own: its report row."""
    _, _, n, (make, check, work) = next(c for c in CASES if c[0] == name)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="ktpu_measure_"))
    path = name if level == "l1" else compiled(name, n, tmp)
    log = pathlib.Path(out) / f"{name}.{level}.model.log"
    card = VerilatorCard(build, nodes=nodes, log=log, timeout=3600.0)
    ins, ref = make(np.random.default_rng(7))
    t0 = time.monotonic()
    res = run(card, path, ins)
    stats = card.close()
    beats = sum(s.get("vres", {}).get("lanes", 0) for s in stats.values()) // 2
    cycles = res["cycles"]
    kind, amount = work
    eff = {
        "mac": amount / (1024 * 4 * len(nodes) * cycles) if amount else None,
        "lane": beats / (16 * 2 * len(nodes) * cycles),
        "bytes": (amount or 0) / cycles,
    }[kind]
    return {
        "example": name,
        "level": level,
        "nodes": len(nodes),
        "cycles": cycles,
        "cold": res["cold"],
        "per_node": res["nodes"],
        "efficiency": eff,
        "metric": {"mac": "MFU", "lane": "lane use", "bytes": "B/cycle"}[kind],
        "rel_err": check(ref, res["out"]),
        "wall_s": round(time.monotonic() - t0, 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", required=True)
    ap.add_argument("--nodes", default="0")
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default=None, help="comma-separated example names")
    ap.add_argument("--jobs", type=int, default=32, help="cards run at once")
    a = ap.parse_args()
    nodes = tuple(int(n) for n in a.nodes.split(","))
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    only = set(a.only.split(",")) if a.only else None
    todo = [
        (name, level)
        for name, levels, n, _ in CASES
        if n == len(nodes) and not (only and name not in only)
        for level in levels
    ]
    # Every case on a card of its own, all at once: a case is one process.
    with ProcessPoolExecutor(max_workers=max(1, min(a.jobs, len(todo)))) as pool:
        futs = [
            pool.submit(measure, a.build, nodes, nm, lv, str(out)) for nm, lv in todo
        ]
        rows = []
        for f in futs:
            rows.append(f.result())
            print(json.dumps(rows[-1]), flush=True)
    (out / "report.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
    lines = [
        "| example | level | nodes | cycles | efficiency | rel err |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        e = r["efficiency"]
        eff = (
            f"{e:.1%} {r['metric']}" if r["metric"] != "B/cycle" else f"{e:.1f} B/cycle"
        )
        lines.append(
            f"| {r['example']} | {r['level']} | {r['nodes']} | {r['cycles']:,} | {eff} | {r['rel_err']:.1e} |"
        )
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["CASES", "main", "measure"]
