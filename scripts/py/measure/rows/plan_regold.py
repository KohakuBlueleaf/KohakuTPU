"""Regold the plan goldens the rows packaging default moves
(`rows.SINGLE_PACKAGE_AST` on: a reduction across column blocks or down the
rows as one `reductions.*` launch), and record the plan goldens of the
ported vectors that name the knob off.

Recordings: `planner_record --select reductions.` at the defaults
(`default/`), then the same goldens under `rows/separate-launches` (the
default off, `before/`).

Reverse proof: every `before/` recording of an existing golden is that
golden as `ktpu-plan`'s tests compare them (`plan_goldens.normal`); a new
golden's `before/` recording is the golden it is recorded from, under its
own name.

Prediction: a recording holding no `reductions.` launch is its `before/`
recording. Else each `reductions.*` launch's graph is Python's emitter
(`launch_reductions.call_graph`) on its call, every other launch is
`before/`'s next launch with that call and graph, in order, and each run,
direct rows call and kp entry ends with `before/`'s records and error.

Nothing is written unless every file verifies. A written golden keeps the
old graph wherever only `where` differs, and takes Python's graph for a
`reductions.*` launch. Originals go to `OUTPUT/originals/plan`, the
manifest (each file's launches, bytes and sha256) to
`OUTPUT/manifest.json`.

    python scripts/py/measure/rows/plan_regold.py --output DIR
"""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from kohakutpu.application.runtime import launch_reductions as LR
from kohakutpu.application.runtime.machine import machine_of

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from measure import plan_goldens as PG

VECTORS = PG.ROOT / "software/tests/spec/vectors"
MUTANT = "rows/separate-launches"
SELECT = "reductions."


def twin(name):
    """The vector whose plan golden vector `name` (no golden) is recorded
    from: the one with its input less `knobs` and a golden."""
    inputs = {
        p.stem: json.loads(p.read_text("utf-8")).get("input")
        for p in VECTORS.rglob("*.json")
    }
    plain = {k: v for k, v in inputs[name].items() if k != "knobs"}
    (got,) = [
        n for n, i in inputs.items() if i == plain and (PG.PLAN / f"{n}.json").exists()
    ]
    return got


def launches(rec):
    return [s["launch"] for e in PG.entries(rec) for s in e["steps"] if "launch" in s]


def check_steps(steps, old_steps, machine):
    """Check `steps` against `old_steps` and Python's emitters, each
    `reductions.*` graph made Python's; how many such launches."""
    n = 0
    rest = iter([s["launch"] for s in old_steps if "launch" in s])
    for launch in [s["launch"] for s in steps if "launch" in s]:
        gen = launch["call"]["gen"] if launch.get("call") else None
        if gen is not None and gen.startswith(SELECT):
            graph = LR.call_graph(gen, launch["call"]["args"], machine)
            assert PG.bare(graph) == PG.bare(launch["graph"]), launch["name"]
            assert graph["name"] == launch["name"], launch["name"]
            launch["graph"] = graph
            n += 1
            continue
        want = (launch.get("call"), PG.bare(launch.get("graph")))
        assert any(
            (x.get("call"), PG.bare(x.get("graph"))) == want for x in rest
        ), launch["name"]
    return n


def predict(default, before, machine):
    """`default` checked against `before` and Python's emitters, each
    `reductions.*` graph Python's; how many such launches."""
    n = 0
    for new, old in zip(PG.entries(default), PG.entries(before), strict=True):
        assert {k: v for k, v in new.items() if k != "steps"} == {
            k: v for k, v in old.items() if k != "steps"
        }
        n += check_steps(new["steps"], old["steps"], machine)
    others = {k for k in default if k not in ("runs", "rows", "kp")}
    assert {k: default[k] for k in others} == {k: before[k] for k in others}
    return default, n


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    out = args.output.resolve()
    assert "KTPU_SINGLE_PACKAGE_AST" not in os.environ
    PG.record(out / "default", SELECT)
    names = sorted(p.stem for p in (out / "default").glob("*.json"))
    PG.record(
        out / "before",
        SELECT,
        "--mutant",
        MUTANT,
        *[x for n in names for x in ("--only", n)],
    )
    writes, files = [], []
    for name in names:
        golden = PG.PLAN / f"{name}.json"
        default = json.loads((out / "default" / golden.name).read_text("utf-8"))
        before = json.loads((out / "before" / golden.name).read_text("utf-8"))
        if golden.exists():
            raw = golden.read_text(encoding="utf-8")
            old = json.loads(raw)
            style = PG.layout(old, raw)
        else:
            raw = None
            old = json.loads((PG.PLAN / f"{twin(name)}.json").read_text("utf-8"))
            old["case"], old["from"] = default["case"], default["from"]
            style = {"indent": 2, "sort_keys": True}
        assert PG.normal(before) == PG.normal(old), (name, "reverse proof")
        machine = machine_of(old["machine"], nodes=old["nodes"])
        if SELECT in json.dumps(default):
            predicted, n = predict(default, before, machine)
        else:
            assert PG.bare(default) == PG.bare(before), (name, "unmoved")
            predicted, n = before, 0
        text = json.dumps(PG.keep_old(old, predicted), **style) + "\n"
        writes.append((golden, raw, text))
        files.append(
            {
                "file": "plan/" + golden.name,
                "new": raw is None,
                "reductions": n,
                "launches": len(launches(predicted)),
                "launches_before": len(launches(old)),
                "bytes": len(text.encode()),
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
            }
        )
    for golden, raw, text in writes:
        if raw is not None:
            backup = out / "originals/plan" / golden.name
            backup.parent.mkdir(parents=True, exist_ok=True)
            with backup.open("x", encoding="utf-8") as stream:
                stream.write(raw)
        golden.write_text(text, encoding="utf-8")
    for f in files:
        print(json.dumps(f), flush=True)
    manifest = {"mutant": MUTANT, "select": SELECT, "files": files}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"regolded {len(files)} goldens", flush=True)


if __name__ == "__main__":
    main()
