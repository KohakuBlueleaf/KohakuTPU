"""What each silicon flaw costs a trace: its kernels compiled and timed on the
machine as built and on the same machine with one flaw absent
(`flaws.<id>.present` false), the difference what that flaw's RTL fix buys
(`docs/spec/stack/flaws.md`).

    python software/tests/perf/flaws.py TRACE_DIR --machine multimesh_v9t8 --out OUT.json

Both sides are compiled here from each kernel's L2, so the difference is the
flaw's and not the compiler's since the trace was recorded. Only a flaw handled
in the kernel's L2 or L1 changes what `timed` reads; every other flaw is listed
with where its handling runs, as not modelled.
"""

import argparse
import json
import pathlib
import re
import subprocess
import tempfile

from traces import load, planned, step

from kohakutpu.compiler import _rs

ROOT = pathlib.Path(__file__).resolve().parents[3]
TIMED = ROOT / "software" / "target" / "stage" / "timed"
PARAMS = pathlib.Path(__file__).with_name("params.json")
MACHINES = ROOT / "machines"
#: Where each flaw's handling runs when it is not in the L1 `timed` reads.
UNMODELLED = {
    "F-002": "the L1 image encoder, below the L1 timed reads",
    "F-003": "the runtime's package lowering",
    "F-004": "the runtime's package lowering",
    "F-005": "KohakuAccel OS",
    "F-006": "KohakuAccel OS",
    "F-007": "the firmware and KohakuAccel OS",
}
#: Statements an L2 handling of a mover flaw rewrites.
MOVES = re.compile(r"^\s*(copy|gather|scatter)\b", re.MULTILINE)


def timed(kernels: pathlib.Path, machine: pathlib.Path, out: pathlib.Path) -> None:
    cmd = [str(TIMED), "--machine", str(machine), "--params", str(PARAMS), "--card"]
    subprocess.run(
        cmd + ["--out", str(out), str(kernels)], check=True, capture_output=True
    )


def cycles(path: pathlib.Path) -> dict[str, float]:
    rows = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
    return {r["name"]: r.get("cycles", 0.0) for r in rows}


def without(machine: dict, fid: str) -> dict:
    out = json.loads(json.dumps(machine))
    out["machine"]["flaws"][fid]["present"] = False
    out["name"] = f"{machine['name']}-no-{fid}"
    return out


def compiled(kernels: dict[str, str], docs: list[dict], out: pathlib.Path) -> list:
    """Each kernel compiled to L1 on each machine of `docs`, in one parallel
    batch, and timed: per machine, name -> cycles."""
    names = list(kernels)
    texts = [json.dumps(d) for d in docs]
    got = _rs.compile_many(
        [kernels[n] for n in names for _ in texts], [t for _ in names for t in texts]
    )
    found = []
    for j, text in enumerate(texts):
        side = out / str(j)
        (side / "l1").mkdir(parents=True)
        (side / "machine.json").write_text(text, "utf-8")
        for i, name in enumerate(names):
            (side / "l1" / f"{name}.l1").write_text(
                got[i * len(texts) + j]["l1"], "utf-8"
            )
        timed(side / "l1", side / "machine.json", side / "timed.jsonl")
        found.append(cycles(side / "timed.jsonl"))
    return found


def report(trace: pathlib.Path, machine: str) -> dict:
    doc = json.loads((MACHINES / f"{machine}.json").read_text("utf-8"))
    launches = load(trace, machine)
    meta = json.loads((trace / "meta.json").read_text("utf-8"))
    warm = step(launches, planned(meta["args"]))
    step_cycles = sum(x.cycles for x in warm)
    moving = {}
    for name in sorted({x.name for x in warm}):
        l2 = (trace / "kernels" / f"{name}.l2").read_text("utf-8")
        if MOVES.search(l2):
            moving[name] = l2
    modelled = [
        fid
        for fid, f in doc["machine"]["flaws"].items()
        if f["present"] and fid not in UNMODELLED
    ]
    with tempfile.TemporaryDirectory() as tmp:
        sides = compiled(
            moving, [doc] + [without(doc, fid) for fid in modelled], pathlib.Path(tmp)
        )
    built = sides[0]
    fixes = dict(zip(modelled, sides[1:]))
    flaws = {}
    for fid, flaw in doc["machine"]["flaws"].items():
        row = dict(
            present=flaw["present"], unit=flaw["unit"], handling=flaw["handling"]
        )
        flaws[fid] = row
        if not flaw["present"]:
            continue
        if fid in UNMODELLED:
            row.update(modelled=False, where=UNMODELLED[fid])
            continue
        fixed = fixes[fid]
        changed = {n for n in built if fixed[n] != built[n]}
        extra = sum(built[x.name] - fixed[x.name] for x in warm if x.name in changed)
        row.update(
            modelled=True,
            kernels=len(changed),
            step_launches=sum(x.name in changed for x in warm),
            step_cycles=extra,
            step_share=extra / step_cycles if step_cycles else 0.0,
        )
    return dict(
        trace=trace.name,
        machine=machine,
        step_cycles=step_cycles,
        moving_kernels=len(moving),
        flaws=flaws,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("trace", type=pathlib.Path)
    ap.add_argument("--machine", default="multimesh_v9t8")
    ap.add_argument("--out", type=pathlib.Path)
    a = ap.parse_args()
    r = report(a.trace, a.machine)
    if a.out:
        a.out.write_text(json.dumps(r, indent=1), "utf-8")
    print(
        r["trace"],
        f"step {r['step_cycles'] / 1e6:.2f} M,",
        r["moving_kernels"],
        "kernels move data",
    )
    for fid, f in r["flaws"].items():
        if not f["present"]:
            print(f"  {fid} absent")
        elif not f["modelled"]:
            print(f"  {fid} present, handled in {f['where']}: not modelled")
        else:
            print(
                f"  {fid} {f['kernels']} kernels, {f['step_launches']} step launches,"
                f" {f['step_cycles'] / 1e6:.3f} M a step ({f['step_share']:.2%})"
            )


if __name__ == "__main__":
    main()
