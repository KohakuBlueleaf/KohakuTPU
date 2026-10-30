"""Regold the plan goldens the relayout order moves (`forms.relayout`:
`kernels.relayout` first, `kernels.lanes` on `Unplanned`).

Recordings: `planner_record --select relayout.relayout_kernel` at the
defaults (`default/`), then the same goldens under `relayout/lanes-only`
(every relayout `gen::lanes`', `before/`); both under `prepared/separate`
(`PREPARED` off: its own regold, `prepared_regold.py`, comes after this
one).

Reverse proof: every `before/` recording is its golden as `ktpu-plan`'s
tests compare them (`plan_goldens.want`). A golden recorded before
RUL.020's explicit padded operands, which the tests take only under
`memory/unsafe-bounds` (`run.rs`), is recorded, compared, predicted and
written under that mutant too (`legacy_default/`, `legacy_before/`).

Prediction, from `before/` and Python alone: each `lanes.relayout_kernel`
launch whose walk pair `R.plan` plans (the destination's line pad in front,
as `forms.relayout` plans it) becomes one `relayout.relayout_kernel` launch:
its call the plan's arguments, its graph and name `R.relayout_kernel`'s, its
destination bound from the line, its index and mask tables `R.plan`'s
(uploaded before it unless already uploaded in the run, as `forms.table`
keys them); every other step stays, a table step only while a launch still
binds it. Each run's records and error, and everything outside the steps,
are `before/`'s.

Nothing is written unless every file verifies. A written golden keeps the
old graph wherever only `where` differs and takes Python's graph for each
moved launch. Originals go to `OUTPUT/originals/plan`, the manifest (each
file's moved launches, bytes and sha256) to `OUTPUT/manifest.json`.

    python scripts/py/measure/relayout/plan_regold.py --output DIR
"""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

import numpy as np
from kohakutpu.application.kernels import relayout as R
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.language import machine as MF
from kohakutpu.stack import kernel as K

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from measure import plan_goldens as PG

MUTANT = "relayout/lanes-only"
SEPARATE = "prepared/separate"
LEGACY = "memory/unsafe-bounds"
SELECT = "relayout.relayout_kernel"


def tables_of(launch) -> list:
    return [b["buf"] for b in launch["binds"].values() if b["buf"].startswith("table:")]


class Mover:
    """`forms.relayout`'s order for one machine."""

    def __init__(self, machine):
        self.machine = machine
        self.hw = R.hw_of(MF.load(machine.board), machine.count("VC"))
        self.moved = 0

    def move(self, launch):
        """The `relayout.relayout_kernel` launch and its two tables replacing
        lanes launch `launch`, or None when `R.plan` does not plan it."""
        a = launch["call"]["args"]["pl"]["made_by"]["args"]
        dst = launch["binds"]["dst"]
        hw = self.hw
        pad = dst["offset"] % (hw.line * hw.word) // 2
        walk = tuple(map(tuple, a["walk"]))
        dwalk = tuple(map(tuple, a["dwalk"]))
        try:
            pl = R.plan(
                a["src_n"], walk, a["at"], dwalk, a["dat"] + pad, a["m"] + pad,
                a["keep"], hw, pad,
            )  # fmt: skip
        except R.Unplanned:
            return None
        k, given = R.relayout_kernel(pl, hw)
        graph = json.loads(
            k.trace(given, machine=K.dsl_machine(self.machine)).graph_json
        )
        index, mask = PG.table(pl.index.astype(np.float32)), PG.table(pl.masks)
        args = {
            "src_n": a["src_n"],
            "walk": a["walk"],
            "at": a["at"],
            "dwalk": a["dwalk"],
            "dat": a["dat"] + pad,
            "m": a["m"] + pad,
            "keep": a["keep"],
            "z0": pad,
            "tree": R.PULL_TREE,
        }
        new = {
            "call": {
                "gen": "relayout.relayout_kernel",
                "args": {"pl": {"made_by": {"gen": "relayout.plan", "args": args}}},
            },
            "name": graph["name"],
            "kind": "convert",
            "info": {},
            "consts": {},
            "binds": {
                "dst": {**dst, "offset": dst["offset"] - 2 * pad},
                "index": index["table"]["at"],
                "mask": mask["table"]["at"],
                "src": launch["binds"]["src"],
            },
            "graph": graph,
        }
        self.moved += 1
        return new, [index, mask]

    def steps(self, old):
        """`old`'s steps with each lanes relayout `move` moves: the table
        steps right before it (its index and mask) replaced by its new
        tables'."""
        out, tables = [], []
        for s in old:
            if "table" in s:
                tables.append(s)
                continue
            launch = s.get("launch")
            got = None
            if (
                launch
                and (launch.get("call") or {}).get("gen") == "lanes.relayout_kernel"
            ):
                got = self.move(launch)
            if got is not None:
                uploaded = sorted(t["table"]["at"]["buf"] for t in tables)
                assert uploaded == sorted(tables_of(launch)), launch["name"]
                out += got[1] + [{**s, "launch": got[0]}]
            else:
                out += tables + [s]
            tables = []
        return out + tables


def predict(before, machine):
    """`before` with its relayouts moved (`Mover`); how many moved."""
    mover = Mover(machine)
    got = json.loads(json.dumps(before))
    for entry in PG.entries(got):
        entry["steps"] = mover.steps(entry["steps"])
    return got, mover.moved


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    out = args.output.resolve()
    assert "KTPU_PULL_TREE" not in os.environ
    PG.record(out / "default", SELECT, "--mutant", SEPARATE)
    names = []
    for p in sorted((out / "default").glob("*.json")):
        if (PG.PLAN / p.name).exists():
            names.append(p.stem)
        else:
            # A knob vector's golden planner_record records from its twin:
            # not this regold's (the twin's is).
            print(json.dumps({"skipped": p.stem}), flush=True)

    def only(ns):
        return [x for n in ns for x in ("--only", n)]

    def load(prefix, kind, name):
        return json.loads((out / f"{prefix}{kind}" / f"{name}.json").read_text("utf-8"))

    lanes = ("--mutant", SEPARATE, "--mutant", MUTANT)
    PG.record(out / "before", SELECT, *lanes, *only(names))
    olds = {n: (PG.PLAN / f"{n}.json").read_text(encoding="utf-8") for n in names}
    legacy = [
        n
        for n in names
        if PG.normal(load("", "before", n)) != PG.want(json.loads(olds[n]))
    ]
    if legacy:
        bounds = ("--mutant", LEGACY)
        PG.record(
            out / "legacy_default", SELECT, "--mutant", SEPARATE, *bounds, *only(legacy)
        )
        PG.record(out / "legacy_before", SELECT, *lanes, *bounds, *only(legacy))
    writes, files, failed = [], [], []
    for name in names:
        golden = PG.PLAN / f"{name}.json"
        prefix = "legacy_" if name in legacy else ""
        default, before = load(prefix, "default", name), load(prefix, "before", name)
        raw = olds[name]
        old = json.loads(raw)
        style = PG.layout(old, raw)
        if PG.normal(before) != PG.want(old):
            failed.append((name, "reverse proof"))
            continue
        machine = machine_of(old["machine"], nodes=old["nodes"])
        predicted, n = predict(before, machine)
        if PG.bare(default) != PG.bare(predicted):
            failed.append((name, "prediction"))
            continue
        text = json.dumps(PG.keep_old(old, predicted), **style) + "\n"
        writes.append((golden, raw, text))
        files.append(
            {
                "file": "plan/" + golden.name,
                "moved": n,
                "legacy": name in legacy,
                "bytes": len(text.encode()),
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
            }
        )
    for name, what in failed:
        print(json.dumps({"failed": name, "check": what}), flush=True)
    assert not failed, f"{len(failed)} of {len(names)} goldens do not verify"
    for golden, raw, text in writes:
        backup = out / "originals/plan" / golden.name
        backup.parent.mkdir(parents=True, exist_ok=True)
        with backup.open("x", encoding="utf-8") as stream:
            stream.write(raw)
        golden.write_text(text, encoding="utf-8")
    for f in files:
        print(json.dumps(f), flush=True)
    moved = sum(f["moved"] for f in files)
    manifest = {"mutant": MUTANT, "select": SELECT, "moved": moved, "files": files}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"regolded {len(files)} goldens, {moved} relayouts moved", flush=True)


if __name__ == "__main__":
    main()
