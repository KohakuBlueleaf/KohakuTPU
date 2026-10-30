"""Paired prepared epilogue-extra model artifacts, including real packed inputs."""

import argparse
import json
from pathlib import Path

import numpy as np
from application.tests.runtime.test_packing_model import reference
from export_folds import expression
from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.kernels import relayout as RL
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import generated
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.language import machine as MF
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs
from package_stage import stage as package_stage


def columns(e):
    """The event's one-dimensional inputs."""
    return [
        p
        for p in e["ports"]
        if p["name"].startswith("in") and len(p["spec"]["shape"]) == 1
    ]


def record(e, metadata, l, convert_key, rng, library):
    """The record of matmul event `e`, its generator arguments `metadata`, whose
    extra input comes from layout `l` of event key `convert_key`; the stages
    prebuild into the graph library at `library`."""
    ci = e["index"]
    hw = RL.hw_of(MF.load("vlt_v9t6s_1n"), 1)
    machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    cols = columns(e)
    assert len(cols) == 1
    port = cols[0]["name"]
    i = int(port[2:])
    pl = RL.plan(
        l["n"],
        tuple(map(tuple, l["walk"])),
        l["at"],
        tuple(map(tuple, l["dwalk"])),
        l["dat"],
        l["m"],
        False,
        hw,
    )
    mm = MM.plan(**metadata["mm"]["made_by"]["args"], machine_json=machine.machine_json)
    expr = expression(metadata["e"])
    extras = tuple(Operand(**o) for o in metadata["extras"])
    out = Operand(**metadata["out"])
    k, g = RL.relayout_kernel(pl, hw)
    c = K.specialise(k, g, machine=machine)
    binds = {
        "src": f"p{i}_x",
        "dst": port,
        "index": f"p{i}_index0",
        "mask": f"p{i}_mask0",
    }
    allocations = {binds[p]: s.nbytes() for p, s in g.items()}
    tables = {
        f"p{i}_index0": pl.index.astype("float32").tobytes().hex(),
        f"p{i}_mask0": pl.masks.tobytes().hex(),
    }
    before = [package_stage(c, binds, library)]
    for prepared in (False, True):
        k, g = MM.mm_kernel(
            mm,
            expr,
            extras,
            out,
            1,
            1,
            made={i: (pl,)} if prepared else None,
            hw=hw if prepared else None,
        )
        if prepared:
            c = K.specialise(k, g, machine=machine)
        else:
            graph = generated.graph(
                "matmul.mm_kernel",
                json.dumps(metadata, sort_keys=True),
                rs.machine_json(machine),
                None,
            )
            c = K.compile(graph, name=json.loads(graph)["name"], machine=machine)
        stage = package_stage(c, {p: p for p in g}, library)
        allocations.update({p: s.nbytes() for p, s in g.items()})
        if prepared:
            after = stage
        else:
            before.append(stage)
        for p, side, group in [("a", "a", mm.tm // 4), ("b", "b", mm.tn // 4)]:
            a = rng.uniform(-1, 1, g[p].shape).astype(np.float16)
            if p not in tables:
                tables[p] = reference(a, side, group, mm.nk).hex()
    step = {k: l[k] for k in ("n", "walk", "at", "dwalk", "dat", "m")}
    recipe = {
        "mm": vars(mm),
        "e": metadata["e"],
        "extras": metadata["extras"],
        "out": metadata["out"],
        "steps": {i: [step]},
    }
    return {
        "index": ci,
        "kind": "epilogue",
        "consumer_key": e["key"],
        "converts": [convert_key],
        "chain_indices": [l["start"], ci],
        "recipe": recipe,
        "allocations": allocations,
        "tables": tables,
        "before": before,
        "after": after,
        "output": "out",
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--work",
        type=Path,
        required=True,
        help="pipe work tree: reads loop5/selected, writes loop5/chains",
    )
    args = ap.parse_args()
    library = args.work / "loop6/pair-library"
    base = args.work / "loop5/selected"
    chains = args.work / "loop5/chains"
    chains.mkdir(parents=True, exist_ok=True)
    es = json.loads((base / "events.json").read_text())
    ls = json.loads((base / "layouts.json").read_text())
    ms = {e["index"]: e["generator_arguments"] for e in es if e["kind"] == "matmul"}
    rng = np.random.default_rng(124)
    for ci, metadata in ms.items():
        e = es[ci]
        if e["capture_pass"] != 1 or e["phase"] != "jit_step2":
            continue
        if not columns(e):
            continue
        at = columns(e)[0]["address"]
        l = next(l for l in reversed(ls) if l["stop"] <= ci and l["dst"] == at)
        rec = record(e, metadata, l, es[l["start"]]["key"], rng, library)
        (chains / f"{ci}.json").write_text(json.dumps(rec))
        print(
            ci,
            rec["chain_indices"],
            [s["verdict"]["refused"] for s in rec["before"] + [rec["after"]]],
            flush=True,
        )


if __name__ == "__main__":
    main()
