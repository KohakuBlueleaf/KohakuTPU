"""Native A/B: standalone relayout(s)+consumer versus prepared consumer."""

import argparse
import json
from pathlib import Path

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.kernels import prepared as P
from kohakutpu.application.kernels import relayout as RL
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import launch_reductions as LR
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.language import machine as MF
from kohakutpu.stack import kernel as K
from package_stage import stage


def expression(doc):
    nodes = []
    for n in doc["nodes"]:
        if "load" in n:
            v = X.Load(n["load"])
        elif "const" in n:
            v = X.Const(float(n["const"]))
        elif "reduce" in n:
            v = X.Reduce(n["reduce"], nodes[n["arg"]])
        else:
            v = X.Op(n["op"], tuple(nodes[i] for i in n["args"]))
        nodes.append(v)
    return nodes[doc["root"]]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("kind", choices=("rows/DSL", "quant"))
    ap.add_argument("--index", type=int)
    ap.add_argument(
        "--work", type=Path, required=True, help="pipe work tree (uses loop4/)"
    )
    args = ap.parse_args()
    library = args.work / "loop6/pair-library"
    base = args.work / "loop4"
    es = json.loads((base / "capture/events.json").read_text())
    ls = json.loads((base / "capture/layouts.json").read_text())
    cs = json.loads((base / "classification.json").read_text())
    trees = {
        r["index"]: r for r in json.loads((base / "capture/trees.json").read_text())
    }
    quants = {
        r["index"]: r for r in json.loads((base / "capture/quants.json").read_text())
    }
    groups = {}
    for c in cs:
        if c["group"] == args.kind and len(c["consumers"]) == 1:
            groups.setdefault(c["consumers"][0]["index"], []).append(c)
    machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    hw = RL.hw_of(MF.load("vlt_v9t6s_1n"), 1)
    output = base / "folds"
    output.mkdir(exist_ok=True)
    for ci, converts in groups.items():
        if args.index is not None and ci != args.index:
            continue
        if args.kind == "rows/DSL" and ci not in trees:
            print(ci, "not column tree", flush=True)
            continue
        c = trees[ci] if args.kind == "rows/DSL" else quants[ci]
        consumer = es[ci]
        plans, allocations, stages, tables = {}, {}, [], {}
        for conv in converts:
            lay = next(
                l
                for l in ls
                if l["start"] == conv["index"] and l["stop"] == conv["index"] + 1
            )
            port = next(
                p["name"]
                for p in consumer["ports"]
                if p["role"] == "in" and p["address"] == lay["dst"]
            )
            i = int(port[2:]) if args.kind == "rows/DSL" else 0
            pl = RL.plan(
                lay["n"],
                tuple(map(tuple, lay["walk"])),
                lay["at"],
                tuple(map(tuple, lay["dwalk"])),
                lay["dat"],
                lay["m"],
                False,
                hw,
            )
            assert P.direct_destination((pl,))
            plans[i] = (pl,)
            k, given = RL.relayout_kernel(pl, hw)
            prefix = f"p{i}_" if args.kind == "rows/DSL" else "p_"
            binds = {
                "src": prefix + "x",
                "dst": port,
                "index": prefix + "index0",
                "mask": prefix + "mask0",
            }
            tables[prefix + "index0"] = pl.index.astype("float32").tobytes().hex()
            tables[prefix + "mask0"] = pl.masks.tobytes().hex()
            compiled = K.specialise(k, given, machine=machine)
            stages.append(stage(compiled, binds, library))
            for p, spec in given.items():
                allocations[binds[p]] = max(allocations.get(binds[p], 0), spec.nbytes())
        if args.kind == "rows/DSL":
            ops = tuple(Operand(**o) for o in c["operands"])
            params = (expression(c["e"]), ops, c["R"], c["W"], Operand(**c["out"]), 1)
            before, bg = LR.column_tree_kernel(*params)
            after, ag = LR.column_tree_kernel(*params, made=plans, hw=hw)
        else:
            params = (
                Operand("full", pitch=c["pitch"], offset=c["offset"]),
                *[
                    c[n]
                    for n in ("rows", "cols", "rp", "cp", "side", "g", "nk", "batches")
                ],
            )
            before, bg = MM.quant_kernel(*params)
            after, ag = MM.quant_kernel(*params, prepared=plans[0], hw=hw)
        for specs in (bg, ag):
            for p, spec in specs.items():
                allocations[p] = max(allocations.get(p, 0), spec.nbytes())
        baseline = K.specialise(before, bg, machine=machine)
        folded = K.specialise(after, ag, machine=machine)
        stages.append(stage(baseline, {p: p for p in bg}, library))
        final = stage(folded, {p: p for p in ag}, library)
        record = {
            "index": ci,
            "kind": args.kind,
            "converts": [r["key"] for r in converts],
            "convert_indices": [r["index"] for r in converts],
            "consumer_key": consumer["key"],
            "allocations": allocations,
            "tables": tables,
            "before": stages,
            "after": final,
            "output": "out" if args.kind == "rows/DSL" else "q",
        }
        (output / f"{ci}.json").write_text(json.dumps(record))
        print(
            ci,
            len(converts),
            [s["verdict"]["refused"] for s in stages],
            folded.verdict["refused"],
            flush=True,
        )


if __name__ == "__main__":
    main()
