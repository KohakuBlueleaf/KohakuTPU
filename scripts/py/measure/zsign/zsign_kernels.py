"""zero_sign on the v9t6 card model (L2.VC.026): each golden kernel whose L1
differs between native and preserve, run under both on the same inputs.

    zsign_kernels.py run --cost DIR --output FILE.jsonl [--budget S] [--shard I/N]
    zsign_kernels.py report --cost DIR --output FILE.jsonl
    zsign_kernels.py show --cost DIR --output FILE.jsonl --case CASE

`show` runs one case under both values and prints its L2, its inputs and
every output element that differs in value.

`run` resumes from FILE and its shard files FILE.sI; `--shard I/N` runs the
I-th of N interleaved parts of the kernels left (one model each) and writes
FILE.sI. A +0 / -0 pair is the same number, so its error
against any reference is the same under both values; `report` counts those
apart from elements that differ in value.
"""

import argparse
import contextlib
import json
import re
import time
from pathlib import Path

import numpy as np
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.packages import Refused
from kohakutpu.language.text.syntax import TextError
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs

PARAM = re.compile(r"(\w+)\s*:\s*(\w+)\[([^\]]*)\]\s*@dram")
ITEM = {
    "f16": 2,
    "f32": 4,
    "bf16": 2,
    "i32": 4,
    "u32": 4,
    "i16": 2,
    "u16": 2,
    "i8": 1,
    "u8": 1,
}
FLOAT = {"f16": np.float16, "f32": np.float32}
EDGES = [0.0, -0.0, 1.0, -1.0, 2.0**-24, -(2.0**-24), np.inf, -np.inf, np.nan]
# A parameter a gather or scatter reads its row indices from.
INDEX_PORT = re.compile(r"\bindex=(\w+)\[")


def cost_lines(d: Path):
    """case -> {value: line} over every cost shard of `d`."""
    out = {}
    for f in sorted(d.glob("cost-*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            out.setdefault(r["case"], {})[r["zero_sign"]] = r
    return out


def differing(lines):
    """Cases whose two values lower to different L1 text (the native L2 kept)."""
    return {
        case: v["native"]
        for case, v in sorted(lines.items())
        if "l1" in v.get("native", {})
        and v["native"]["l1"] != v.get("preserve", {}).get("l1")
    }


def params(l2):
    """(name, dtype, element count) of each @dram parameter of the fn line."""
    head = next(l for l in l2.splitlines() if l.lstrip().startswith("fn "))
    out = []
    for name, dtype, shape in PARAM.findall(head):
        dims = [int(s) for s in shape.split(",")]
        out.append((name, dtype, int(np.prod(dims))))
    return out


def data(dtype, n, rng):
    """`n` seeded elements of `dtype` as bytes; floats hold an edge value
    every 16 elements."""
    if dtype in FLOAT:
        x = rng.standard_normal(n) * 4.0
        x[::16] = np.resize(EDGES, x[::16].size)
        return x.astype(FLOAT[dtype]).tobytes()
    return rng.integers(0, 256, n * ITEM[dtype], dtype=np.uint8).tobytes()


def index_data(dtype, n, rng):
    """`n` seeded elements of `dtype` that an index port takes: the whole
    numbers 0 to 7 (the mover faults on a float index that is not one)."""
    if dtype in FLOAT:
        return rng.integers(0, 8, n).astype(FLOAT[dtype]).tobytes()
    return data(dtype, n, rng)


def inputs_of(l2, ps):
    """Seeded inputs for the parameters `ps` of `l2`: index data on every
    gather / scatter index port, edge-valued data on the rest."""
    idx = set(INDEX_PORT.findall(l2))
    rng = np.random.default_rng(58)
    return {name: (index_data if name in idx else data)(d, n, rng) for name, d, n in ps}


def run_one(dev, kernel, l2, zero_sign, inputs):
    with rs.zero_sign(zero_sign):
        c = K.compile(l2, name=kernel, machine=dev.machine)
    binds, sizes = {}, {}
    for name, raw in inputs.items():
        a = dev.alloc(len(raw))
        dev.write(a, raw)
        binds[name], sizes[name] = a, len(raw)
    try:
        before = dev.counters["cycles"]
        dev.run(c, binds)
        cycles = dev.counters["cycles"] - before
        out = {name: dev.read(a, sizes[name]) for name, a in binds.items()}
    finally:
        for a in binds.values():
            dev.free(a)
    return cycles, out


def both(dev, line, inputs):
    """{value: (cycles, outputs) or (None, why)} for both values, and the
    device to go on with."""
    got = {}
    for z in ("native", "preserve"):
        try:
            got[z] = run_one(dev, line["kernel"], line["l2"], z, inputs)
        except (TextError, Refused) as e:
            got[z] = (None, f"{type(e).__name__}: {e}"[:400])
        except (RuntimeError, ValueError) as e:
            # A run fault poisons every later run on this device, and closing
            # it drains, which raises on a faulted device.
            got[z] = (None, f"{type(e).__name__}: {e}"[:400])
            with contextlib.suppress(RuntimeError):
                dev.close()
            dev = D.attach(D.Device())
    return got, dev


def compare(dtype, a: bytes, b: bytes):
    """(elements, differing only as +0 / -0, differing in value)."""
    if dtype not in FLOAT:
        return len(a), 0, int(a != b)
    x = np.frombuffer(a, FLOAT[dtype])
    y = np.frombuffer(b, FLOAT[dtype])
    bx = x.view(np.uint16 if dtype == "f16" else np.uint32)
    by = y.view(bx.dtype)
    differ = bx != by
    nan = np.isnan(x) & np.isnan(y)
    zeros = differ & (x == 0) & (y == 0)
    return x.size, int(zeros.sum()), int((differ & ~zeros & ~nan).sum())


def rows_of(output: Path):
    """Every row of FILE and of its shard files."""
    files = [output, *sorted(output.parent.glob(f"{output.stem}.s*{output.suffix}"))]
    return [
        json.loads(l)
        for f in files
        if f.exists()
        for l in f.read_text(encoding="utf-8").splitlines()
        if l
    ]


def run(args):
    todo = differing(cost_lines(args.cost))
    done, seen = set(), {}
    for r in rows_of(args.output):
        done.add(r["case"])
        seen[(todo.get(r["case"], {}).get("l2"), r["machine"])] = r
    i, n = (int(s) for s in args.shard.split("/"))
    out = args.output
    if n > 1:
        out = out.with_name(f"{out.stem}.s{i}{out.suffix}")
    # Shards split the kernels by their place in the whole list, so shards
    # started together never take the same kernel.
    mine = {c for k, c in enumerate(todo) if k % n == i}
    # The case a call was running when its call was cut: recorded as cut, so
    # a kernel longer than one call does not hold every later one back.
    pending = out.with_suffix(".pending")
    dev = D.device()
    t0 = time.monotonic()
    with out.open("a", encoding="utf-8") as f:
        cut = pending.read_text() if pending.exists() else ""
        if cut in todo and cut not in done:
            line = todo[cut]
            row = {"case": cut, "kernel": line["kernel"], "machine": line["machine"]}
            row["cut"] = "did not finish within one call"
            f.write(json.dumps(row) + "\n")
            f.flush()
            done.add(cut)
        for case, line in todo.items():
            if case in done or case not in mine:
                continue
            if time.monotonic() - t0 > args.budget:
                break
            pending.write_text(case)
            row = {"case": case, "kernel": line["kernel"], "machine": line["machine"]}
            ps = params(line["l2"])
            same = seen.get((line["l2"], line["machine"]))
            if same is not None:
                # The same kernel text on the same machine (a case under two
                # golden trees) runs once; this row is that run's.
                row.update({k: v for k, v in same.items() if k not in row})
                row["same_as"] = same["case"]
            elif any(d not in ITEM for _, d, _ in ps):
                row["skipped"] = f"dtype {sorted({d for _, d, _ in ps} - set(ITEM))}"
            else:
                got, dev = both(dev, line, inputs_of(line["l2"], ps))
                row["cycles"] = {z: got[z][0] for z in got}
                if any(c is None for c, _ in got.values()):
                    row["refused"] = {z: got[z][1] for z in got if got[z][0] is None}
                else:
                    row["ports"] = {
                        name: dict(
                            zip(
                                ("elements", "signed_zero", "value"),
                                compare(
                                    d, got["native"][1][name], got["preserve"][1][name]
                                ),
                            )
                        )
                        for name, d, _ in ps
                    }
            seen.setdefault((line["l2"], line["machine"]), row)
            f.write(json.dumps(row) + "\n")
            f.flush()
            pending.unlink()
            print(json.dumps({k: v for k, v in row.items() if k != "ports"}))
    ran = {r["case"] for r in rows_of(args.output)}
    left = [c for c in todo if c not in ran]
    print(f"zsign_kernels: {len(todo)} differing kernels, {len(left)} left")


def report(args):
    lines = cost_lines(args.cost)
    todo = differing(lines)
    rows = rows_of(args.output)
    value = [
        r["case"] for r in rows if any(p["value"] for p in r.get("ports", {}).values())
    ]
    out = {
        "cases": len(lines),
        "lowered_both": sum(
            "l1" in v.get("native", {}) and "l1" in v.get("preserve", {})
            for v in lines.values()
        ),
        "refused_preserve_only": sorted(
            c
            for c, v in lines.items()
            if "l1" in v.get("native", {}) and "refused" in v.get("preserve", {})
        ),
        "differing_l1": len(todo),
        "differing_detail": {
            case: {
                "search_cycles": {
                    z: lines[case][z]["cycles"] for z in ("native", "preserve")
                },
                "lines": {z: lines[case][z]["lines"] for z in ("native", "preserve")},
                "seconds": {
                    z: round(lines[case][z]["seconds"], 3)
                    for z in ("native", "preserve")
                },
                "ops_delta": {
                    op: n
                    for op in sorted(
                        set(lines[case]["native"]["ops"])
                        | set(lines[case]["preserve"]["ops"])
                    )
                    if (
                        n := lines[case]["preserve"]["ops"].get(op, 0)
                        - lines[case]["native"]["ops"].get(op, 0)
                    )
                },
            }
            for case in todo
        },
        "compile_seconds": {
            z: round(sum(v[z]["seconds"] for v in lines.values() if z in v), 1)
            for z in ("native", "preserve")
        },
        "run": len(rows),
        "skipped": {r["case"]: r["skipped"] for r in rows if "skipped" in r},
        "refused_on_model": {r["case"]: r["refused"] for r in rows if "refused" in r},
        "differ_in_value": value,
        "signed_zero_elements": sum(
            p["signed_zero"] for r in rows for p in r.get("ports", {}).values()
        ),
        "cycles": {r["case"]: r["cycles"] for r in rows if "ports" in r},
    }
    print(json.dumps(out, indent=1))


def show(args):
    line = differing(cost_lines(args.cost))[args.case]
    ps = params(line["l2"])
    inputs = inputs_of(line["l2"], ps)
    got, _ = both(D.device(), line, inputs)
    print(line["l2"])
    with np.printoptions(threshold=100000, linewidth=160):
        for name, d, _ in ps:
            if d not in FLOAT:
                continue
            x = np.frombuffer(inputs[name], FLOAT[d])
            print(f"input {name}: {x}")
            if any(c is None for c, _ in got.values()):
                continue
            a = np.frombuffer(got["native"][1][name], FLOAT[d])
            b = np.frombuffer(got["preserve"][1][name], FLOAT[d])
            ix = np.flatnonzero((a != b) & ~(np.isnan(a) & np.isnan(b)))
            ix = ix[(a[ix] != 0) | (b[ix] != 0)]
            if ix.size and a.size <= 64:
                print(f"{name} native   {a}")
                print(f"{name} preserve {b}")
            print(f"{name} differs at {ix}")
            print(f"  native   {a[ix]}")
            print(f"  preserve {b[ix]}")
    print({z: got[z][0] if got[z][0] is not None else got[z][1] for z in got})


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("action", choices=("run", "report", "show"))
    ap.add_argument("--case")
    ap.add_argument("--cost", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--budget", type=float, default=80)
    ap.add_argument("--shard", default="0/1")
    args = ap.parse_args()
    {"run": run, "report": report, "show": show}[args.action](args)


if __name__ == "__main__":
    main()
