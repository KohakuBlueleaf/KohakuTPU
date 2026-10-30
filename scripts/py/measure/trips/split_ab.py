"""The two trip splits (`vector::Trips`) A/B on the Verilated v9t6 card
model at two vector cores (`vlt_v9t6_2n`, one node): for each generator
that deals odd trips (a transpose, a rows stage, a rows kernel with a tail
band, a relayout, a wide fold), the first candidate shape whose graphs the
two splits trace differently, both compiled and run on the same inputs.
Every output must agree byte for byte; the table gives each split's cycles.

    python scripts/py/measure/trips/split_ab.py --output R.json [--case NAME ...]

One model process an invocation, over the CASES named (all by default);
rows already in R.json are kept and their cases skipped, so a run can be
cut into one case an invocation. The default split is `vector::defaults()`;
the other is `vector/other-split`.
"""

import argparse
import json
from pathlib import Path

import numpy as np
from kohakutpu.application.kernels import relayout as R
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import generated
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.compiler import _rs
from kohakutpu.language import machine as MF
from kohakutpu.stack import rs

ROOT = Path(__file__).resolve().parents[4]
BOARD = "vlt_v9t6_2n"
BUILD = Path.home() / "ktpu/build/t2s/vlt_card_v9t6_2n"
OTHER = "vector/other-split"
MACHINE = machine_of(BOARD, nodes=1)
CORES = MACHINE.count("VC")
SUM = {"nodes": [{"load": 0}, {"arg": 0, "reduce": "sum"}], "root": 1}
NEG = {"nodes": [{"load": 0}, {"args": [0], "op": "neg"}], "root": 1}


def full(pitch):
    return {"kind": "full", "dtype": "f16", "pitch": pitch, "offset": 0, "rows": 0}


ROW = {"kind": "row", "dtype": "f16", "pitch": 0, "offset": 0, "rows": 0}


def transposed(rows, cols):
    n = rows * cols
    args = {
        "src_n": n,
        "walk": [[rows, 1], [cols, rows]],
        "at": 0,
        "dwalk": [[rows, cols], [cols, 1]],
        "dat": 0,
        "m": n,
        "keep": False,
    }
    return {"pl": {"made_by": {"gen": "relayout.plan", "args": args}}}


#: kernel -> (generator, candidate calls), the first the splits trace apart taken
CASES = {
    "transpose": (
        "transpose.transpose_kernel",
        [
            {"nb": nb, "A": a, "B": b}
            for nb in (1, 3, 5)
            for a, b in ((32, 32), (64, 32))
        ],
    ),
    "rows_stage": (
        "vector.rows_kernel",
        [
            {"e": NEG, "operands": [full(1024)], "R": r, "W": 1024, "out": full(1024)}
            for r in range(8, 256, 4)
        ],
    ),
    "tail_band": (
        "vector.rows_kernel",
        [
            {"e": NEG, "operands": [full(1024)], "R": r, "W": 1024, "out": full(1024)}
            for r in range(10, 256, 4)
        ],
    ),
    "relayout": (
        "relayout.relayout_kernel",
        [transposed(r, c) for r, c in ((40, 64), (24, 96), (56, 64), (88, 32))],
    ),
    "wide_fold": (
        "reductions.wide_fold_kernel",
        [
            {
                "e": SUM,
                "operands": [full(w)],
                "R": r,
                "W": w,
                "out": ROW,
                "cores": CORES,
            }
            for r, w in ((8, 1184), (3, 2368), (20, 3552), (12, 1216))
        ],
    ),
}
NBYTES = {"f16": 2, "f32": 4, "i32": 4}


def graph(gen, args, mutant):
    return rs._rs.generate(gen, json.dumps(args), rs.machine_json(MACHINE), mutant)


def bare(node):
    if isinstance(node, dict):
        return {k: bare(v) for k, v in node.items() if k != "where"}
    if isinstance(node, list):
        return [bare(v) for v in node]
    return node


def tables(args):
    """The relayout's index and mask tables, as the planner uploads them."""
    a = args["pl"]["made_by"]["args"]
    hw = R.hw_of(MF.load(MACHINE.board), CORES)
    walk, dwalk = tuple(map(tuple, a["walk"])), tuple(map(tuple, a["dwalk"]))
    pl = R.plan(a["src_n"], walk, a["at"], dwalk, a["dat"], a["m"], a["keep"], hw)
    return {
        "index": np.ascontiguousarray(pl.index.astype(np.float32)).tobytes(),
        "mask": np.ascontiguousarray(pl.masks).tobytes(),
    }


def inputs(source, args, rng):
    """Bytes for each input port: the relayout's tables, else random f16."""
    known = tables(args) if "pl" in args else {}
    out = {}
    for p in json.loads(source)["ports"]:
        if p["role"] != "in":
            continue
        n = int(np.prod(p["spec"]["shape"])) * NBYTES[p["spec"]["dtype"]]
        out[p["name"]] = known.get(p["name"]) or (
            rng.standard_normal(n // 2).astype(np.float16).tobytes()
        )
    return out


def run(dev, source, data):
    """`source` compiled and run twice on `data`: the second run's cycles
    and every output's bytes."""
    output = rs._rs.compile(source, rs.machine_json(MACHINE), cache_dir="", fetch=True)
    assert not output["verdict"]["refused"], output["verdict"]
    comp = generated.from_output(dev, json.loads(source), output)
    ports = json.loads(source)["ports"]
    sizes = {
        p["name"]: int(np.prod(p["spec"]["shape"])) * NBYTES[p["spec"]["dtype"]]
        for p in ports
    }
    binds = {name: dev.alloc(n) for name, n in sizes.items()}
    try:
        cycles = []
        for _ in range(2):
            for name, b in data.items():
                dev.write(binds[name], b)
            for p in ports:
                if p["role"] == "out":
                    dev.write(binds[p["name"]], bytes(sizes[p["name"]]))
            cycles.append(dev.run(comp, binds, kind="dsl").cycles)
        outs = {
            p["name"]: dev.read(binds[p["name"]], sizes[p["name"]])
            for p in ports
            if p["role"] == "out"
        }
        return cycles[-1], outs, len(output["l1"].splitlines())
    finally:
        for a in binds.values():
            dev.free(a)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--case", action="append", choices=list(CASES))
    args = ap.parse_args()
    default = json.loads(_rs.VECTOR_DEFAULTS)["trips"]
    rows = json.loads(args.output.read_text())["rows"] if args.output.exists() else []
    done = {r["kernel"] for r in rows}
    picked = {}
    for name in args.case or CASES:
        if name in done:
            continue
        gen, calls = CASES[name]
        for call in calls:
            a, b = graph(gen, call, None), graph(gen, call, OTHER)
            if bare(json.loads(a)) != bare(json.loads(b)):
                picked[name] = (gen, call, a, b)
                break
        else:
            raise SystemExit(f"{name}: no candidate the two splits trace apart")
    dev = D.attach(D.Device(kind="sim", board=BOARD, build=BUILD, queued=False))
    try:
        for name, (gen, call, a, b) in picked.items():
            # One stream a case: a case's inputs do not hang on which ran before.
            rng = np.random.default_rng([16, list(CASES).index(name)])
            data = inputs(a, call, rng)
            ca, oa, la = run(dev, a, data)
            cb, ob, lb = run(dev, b, data)
            assert oa == ob, f"{name}: the splits' outputs differ"
            other = "balanced" if default == "hchunk" else "hchunk"
            rows.append(
                {
                    "kernel": name,
                    "gen": gen,
                    "call": call,
                    default: {"cycles": ca, "l1_lines": la},
                    other: {"cycles": cb, "l1_lines": lb},
                    "balanced_minus_hchunk": (
                        (cb - ca) if default == "hchunk" else (ca - cb)
                    ),
                }
            )
            print(json.dumps(rows[-1]), flush=True)
            report = {"board": BOARD, "cores": CORES, "default": default, "rows": rows}
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2) + "\n")
    finally:
        dev.close()


if __name__ == "__main__":
    main()
