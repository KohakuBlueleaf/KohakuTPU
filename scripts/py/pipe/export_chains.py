"""Measure current WINDOW_SWAP=0 convert/move chains into quant consumers."""

import argparse
import json
from pathlib import Path

from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.kernels import prepared as P
from kohakutpu.application.kernels import relayout as RL
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import costs
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.language import machine as MF
from kohakutpu.stack import kernel as K
from package_stage import stage


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--index", type=int)
    ap.add_argument(
        "--work",
        type=Path,
        required=True,
        help="pipe work tree: reads loop4/selected-ws0, loop5/card-baseline.json",
    )
    args = ap.parse_args()
    library = args.work / "loop6/pair-library"
    base = args.work / "loop4/selected-ws0"
    output = args.work / "loop5/chains"
    output.mkdir(parents=True, exist_ok=True)
    es = json.loads((base / "events.json").read_text())
    ls = json.loads((base / "layouts.json").read_text())
    qs = {q["index"]: q for q in json.loads((base / "quants.json").read_text())}
    eligibility = json.loads((base / "eligibility.json").read_text())
    sources = json.loads((base / "capture.json").read_text())["sources"]
    hw = RL.hw_of(MF.load("vlt_v9t6s_1n"), 1)
    machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    card = json.loads((args.work / "loop5/card-baseline.json").read_text())
    card_cost = {r["name"][2:]: r["steps"] for r in card["runs"] if r["step"] == 5}
    candidates = [
        q
        for q in eligibility
        if es[q["index"]]["capture_pass"] == 1
        and es[q["index"]]["phase"] == "jit_step2"
        and not q["selected"]
    ]
    candidates.sort(key=lambda q: -card_cost.get(es[q["index"]]["key"], 0))
    for query in candidates:
        index = query["index"]
        if args.index is not None and index != args.index:
            continue
        steps = query["steps"]
        # An identity final walk is already elided by the eager baseline.
        if (
            len(steps) > 1
            and steps[-1]["walk"] == steps[-1]["dwalk"]
            and steps[-1]["at"] == steps[-1]["dat"] == 0
            and steps[-1]["m"] == steps[-1]["n"]
        ):
            steps = steps[:-1]
        chain = [es[index]]
        addr = next(p["address"] for p in chain[-1]["ports"] if p["role"] == "out")
        for e in es[index + 1 :]:
            if e["phase"] != "jit_step2":
                break
            if any(p["role"] == "in" and p["address"] == addr for p in e["ports"]):
                chain.append(e)
                if e["kind"] == "quant":
                    break
                addr = next(p["address"] for p in e["ports"] if p["role"] == "out")
        assert chain[-1]["kind"] == "quant", chain
        ci = chain[-1]["index"]
        q = qs[ci]
        if len(chain) - 1 > len(steps):
            last = steps[-1]
            old_pitch = last["m"] // (q["rows"] * q["batches"])
            new_pitch = q["pitch"]
            assert last["dat"] == 0
            steps = [
                *steps[:-1],
                dict(
                    last,
                    dwalk=[
                        [n, (d // old_pitch) * new_pitch + d % old_pitch]
                        for n, d in last["dwalk"]
                    ],
                    m=q["rows"] * q["batches"] * new_pitch,
                ),
            ]
        plans = tuple(
            RL.plan(
                s["n"],
                tuple(map(tuple, s["walk"])),
                s["at"],
                tuple(map(tuple, s["dwalk"])),
                s["dat"],
                s["m"],
                False,
                hw,
            )
            for s in steps
        )
        assert P.direct_destination(plans)
        tables = {}
        for i, p in enumerate(plans):
            tables[f"p_index{i}"] = p.index.astype("float32").tobytes().hex()
            tables[f"p_mask{i}"] = p.masks.tobytes().hex()
        allocations = {}
        addresses = {}
        before = []
        first = next(p["address"] for p in chain[0]["ports"] if p["name"] == "src")
        addresses[first] = "p_x"
        for j, e in enumerate(chain):
            graph = json.loads(sources[e["key"]]["source"])
            graph["machine"] = {"mg": 1, "vc": 1, "nodes": 1}
            if e["kind"] == "convert":
                lay = next(l for l in ls if l["start"] == e["index"])
                original = RL.plan(
                    lay["n"],
                    tuple(map(tuple, lay["walk"])),
                    lay["at"],
                    tuple(map(tuple, lay["dwalk"])),
                    lay["dat"],
                    lay["m"],
                    False,
                    hw,
                )
                k, g = RL.relayout_kernel(original, hw)
                tables[f"b_index{j}"] = original.index.astype("float32").tobytes().hex()
                tables[f"b_mask{j}"] = original.masks.tobytes().hex()
                compiled = K.specialise(k, g, machine=machine)
            else:
                compiled = K.compile(
                    json.dumps(graph), name=graph["name"], machine=machine
                )
            binds = {}
            for p in e["ports"]:
                n = 2
                for dim in p["spec"]["shape"]:
                    n *= dim
                if p["spec"]["dtype"].startswith("mx7"):
                    n //= 2
                if p["spec"]["dtype"] == "f32":
                    n *= 2
                if p["name"] in ("index", "mask"):
                    label = f"b_{p['name']}{j}"
                else:
                    label = addresses.setdefault(
                        p["address"],
                        "q" if e["kind"] == "quant" and p["role"] == "out" else f"z{j}",
                    )
                binds[p["name"]] = label
                if p["name"] in ("index", "mask"):
                    n = len(bytes.fromhex(tables[label]))
                allocations[label] = max(n, allocations.get(label, 0))
            before.append(stage(compiled, binds, library))
        src = Operand("full", pitch=q["pitch"], offset=q["offset"])
        qr = costs.quant_recipe(
            src,
            *[q[k] for k in ("rows", "cols", "rp", "cp", "side", "g", "nk", "batches")],
        )
        strategy = (
            "staged"
            if costs.choose("staged_padding", qr, "multimesh_v9t6")
            else "native"
        )
        k, g = MM.quant_kernel(
            src,
            *[q[k] for k in ("rows", "cols", "rp", "cp", "side", "g", "nk", "batches")],
            prepared=plans,
            hw=hw,
            pad_strategy=strategy,
        )
        c = K.specialise(k, g, machine=machine)
        for p, s in g.items():
            allocations[p] = max(allocations.get(p, 0), s.nbytes())
        rec = {
            "index": index,
            "kind": "quant-chain",
            "consumer_key": chain[-1]["key"],
            "converts": [e["key"] for e in chain[:-1]],
            "chain_indices": [e["index"] for e in chain],
            "card_steps": sum(card_cost.get(e["key"], 0) for e in chain),
            "recipe": {"steps": steps, "quant": qr, "pad_strategy": strategy},
            "allocations": allocations,
            "tables": tables,
            "before": before,
            "after": stage(c, {p: p for p in g}, library),
            "output": "q",
        }
        (output / f"{index}.json").write_text(json.dumps(rec))
        print(
            index,
            rec["chain_indices"],
            strategy,
            rec["card_steps"],
            [s["verdict"]["refused"] for s in before + [rec["after"]]],
            flush=True,
        )


if __name__ == "__main__":
    main()
