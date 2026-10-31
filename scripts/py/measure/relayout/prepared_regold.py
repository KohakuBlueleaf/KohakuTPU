"""Regold the plan goldens `forms.PREPARED` moves (a pending operand made
inside the kernel that reads it, else by its own relayouts when first read).

Recordings: `planner_record --select steps` (every golden) at the defaults
(`default/`) and under `prepared/separate` (`before/`).

Reverse proof: every `before/` recording is its golden as `ktpu-plan`'s
tests compare them (`plan_goldens.want`). A golden the tests take only
under `memory/unsafe-bounds` (`run.rs`: recorded before RUL.020's explicit
padded operands) is recorded, compared, predicted and written under that
mutant too (`legacy_default/`, `legacy_before/`).

Prediction, for each golden whose default recording differs:
- everything outside the runs' steps, and each run's `end`, is `before/`'s;
- each launch with a `made` argument on `reductions.column_tree_kernel` /
  `matmul.mm_kernel` has Python's graph (`LR.column_tree_kernel`,
  `MM.mm_kernel`; the product's name aside: Python spells its knobs
  differently), its plans `R.plan` of the recorded `relayout.plan` calls;
- a `prepared.rows_kernel` launch has the generator's own graph, which
  `test_prepared_port` pins; its tables are checked as the others';
- each relayout launch is one `before/` launches with the same moves
  (source elements to destination elements, whatever the walks' level
  grouping), each at most once;
- every other launch is one of `before/`'s, graph and call alike;
- every table `default/` uploads, `before/` uploads or a made plan holds.

A knob vector recorded with no golden yet (`..._separate`: `PREPARED` off,
`TG.RUN.015.padding_lanes_masked_separate`) is written as a new golden
when it is its twin's `before/` recording with only its case name and
source changed, and differs from its twin's default recording.

Nothing is written unless every file verifies. A written golden keeps the
old graph wherever only `where` differs. Originals go to
`OUTPUT/originals/plan`, the manifest (each file's made launches, bytes and
sha256) to `OUTPUT/manifest.json`.

    python scripts/py/measure/relayout/prepared_regold.py --output DIR
"""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

import numpy as np
from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.kernels import relayout as R
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.plan import emit
from kohakutpu.application.runtime import launch_reductions as LR
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.language import machine as MF
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from measure import plan_goldens as PG

MUTANT = "prepared/separate"
LEGACY = "memory/unsafe-bounds"
#: every golden holds it
SELECT = "steps"


def plans_of(made_by: list, hw) -> list:
    out = []
    for p in made_by:
        a = p["made_by"]["args"]
        walk, dwalk = tuple(map(tuple, a["walk"])), tuple(map(tuple, a["dwalk"]))
        z0 = a.get("z0", 0)
        out.append(
            R.plan(
                a["src_n"], walk, a["at"], dwalk, a["dat"], a["m"], a["keep"], hw, z0
            )
        )
    return out


def elements(walk, at) -> np.ndarray:
    """The elements `walk` reaches from `at`, in walk order."""
    out = np.array([at], dtype=np.int64)
    for n, s in walk:
        out = (out[:, None] + s * np.arange(n, dtype=np.int64)[None, :]).reshape(-1)
    return out


def walks(made_by_args: dict) -> tuple:
    """A relayout's moves, whatever its levels' grouping: (source elements,
    each destination element's source, in destination order)."""
    a = made_by_args
    src = elements(a["walk"], a["at"])
    dst = elements(a["dwalk"], a["dat"] - a.get("z0", 0))
    order = np.argsort(dst, kind="stable")
    return a["src_n"], dst[order].tobytes(), src[order].tobytes()


def tables(plans) -> set:
    """The table arrays the planner uploads for `plans` (`forms.table`)."""
    out = set()
    for p in plans:
        for arr in (p.index.astype(np.float32), p.masks):
            out.add(json.dumps(PG.table(arr)["table"]["array"]))
    return out


class Checker:
    """Python's graph for each launch of one machine that makes operands."""

    def __init__(self, machine):
        self.machine = machine
        self.cores = machine.count("VC")
        self.hw = R.hw_of(MF.load(machine.board), self.cores)
        self.made = 0

    def graph(self, k, given) -> dict:
        return json.loads(
            k.trace(given, machine=K.dsl_machine(self.machine)).graph_json
        )

    def python(self, call) -> tuple:
        """Python's graph of `call` and whether the name is compared; None
        for a call that makes nothing."""
        gen, a = call["gen"], call["args"]
        made = {int(i): plans_of(ps, self.hw) for i, ps in a.get("made", {}).items()}
        if not made:
            return None
        if gen == "reductions.column_tree_kernel":
            ops = tuple(Operand(**o) for o in a["operands"])
            k = LR.column_tree_kernel(
                emit.parse_expr(a["e"]["expr"]), ops, a["R"], a["W"], Operand(**a["out"]),
                self.cores, made=made, hw=self.hw,
            )  # fmt: skip
            return self.graph(*k), True
        assert gen == "matmul.mm_kernel", gen
        p = a["mm"]["made_by"]["args"]
        mm = MM.plan(
            p["bt"], p["m"], p["n"], p["k"], p["shared"], p.get("quantum", 4),
            machine_json=rs.machine_json(self.machine),
        )  # fmt: skip
        k = MM.mm_kernel(
            mm, emit.parse_expr(a["e"]["expr"]), tuple(Operand(**o) for o in a["extras"]),
            Operand(**a["out"]), self.machine.count("MG"), self.cores,
            made=made, hw=self.hw,
        )  # fmt: skip
        return self.graph(*k), False

    def check(self, name, default, before):
        """Raise unless `default` is `before` with its operands made as the
        prediction says; how many launches make operands."""
        outside = ("runs", "rows", "kp")
        for k in set(default) | set(before):
            if k not in outside:
                assert PG.bare(default.get(k)) == PG.bare(before.get(k)), (name, k)
        made_here = 0
        for rd, rb in zip(PG.entries(default), PG.entries(before), strict=True):
            assert PG.bare(rd.get("end")) == PG.bare(rb.get("end")), (name, "end")
            old = [s["launch"] for s in rb["steps"] if "launch" in s]
            old_bare = [PG.bare({**x, "binds": None}) for x in old]
            relayouts = [
                walks(x["call"]["args"]["pl"]["made_by"]["args"])
                for x in old
                if (x.get("call") or {}).get("gen") == "relayout.relayout_kernel"
            ]
            launches = [s["launch"] for s in rd["steps"] if "launch" in s]
            allowed = {
                json.dumps(s["table"]["array"]) for s in rb["steps"] if "table" in s
            }
            for lc in launches:
                for ps in (
                    (lc.get("call") or {}).get("args", {}).get("made", {}).values()
                ):
                    allowed |= tables(plans_of(ps, self.hw))
            for s in rd["steps"]:
                if "table" in s:
                    assert json.dumps(s["table"]["array"]) in allowed, (name, "table")
            for launch in launches:
                call = launch.get("call") or {}
                if call.get("gen") == "prepared.rows_kernel":
                    made_here += 1
                    continue
                got = self.python(call) if call else None
                if got is not None:
                    want, named = got
                    have = PG.bare(launch["graph"])
                    if not named:
                        have, want = dict(have, name=None), dict(want, name=None)
                    assert have == PG.bare(want), (name, launch["name"], "graph")
                    made_here += 1
                elif call.get("gen") == "relayout.relayout_kernel":
                    key = walks(call["args"]["pl"]["made_by"]["args"])
                    assert key in relayouts, (name, "relayout")
                    relayouts.remove(key)
                else:
                    have = PG.bare({**launch, "binds": None})
                    assert have in old_bare, (name, launch["name"])
        self.made += made_here
        return made_here


def only(names) -> list:
    return [x for n in names for x in ("--only", n)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    out = args.output.resolve()
    assert "KTPU_PREPARED" not in os.environ

    def load(kind, name):
        return json.loads((out / kind / f"{name}.json").read_text("utf-8"))

    PG.record(out / "default", SELECT)
    recorded = sorted(p.stem for p in (out / "default").glob("*.json"))
    twins = [n for n in recorded if not (PG.PLAN / f"{n}.json").exists()]
    names = [n for n in recorded if n not in twins]
    PG.record(out / "before", SELECT, "--mutant", MUTANT)
    olds = {n: (PG.PLAN / f"{n}.json").read_text(encoding="utf-8") for n in names}
    legacy = [
        n for n in names if PG.normal(load("before", n)) != PG.want(json.loads(olds[n]))
    ]
    if legacy:
        PG.record(out / "legacy_default", SELECT, "--mutant", LEGACY, *only(legacy))
        PG.record(
            out / "legacy_before", SELECT, "--mutant", MUTANT, "--mutant", LEGACY,
            *only(legacy),
        )  # fmt: skip
    writes, files, failed = [], [], []
    for name in names:
        prefix = "legacy_" if name in legacy else ""
        default, before = load(prefix + "default", name), load(prefix + "before", name)
        raw = olds[name]
        old = json.loads(raw)
        if PG.normal(before) != PG.want(old):
            failed.append((name, "reverse proof"))
            continue
        if PG.normal(default) == PG.want(old):
            continue
        checker = Checker(machine_of(old["machine"], nodes=old["nodes"]))
        try:
            n = checker.check(name, default, before)
        except AssertionError as e:
            failed.append((name, repr(e)))
            continue
        text = json.dumps(PG.keep_old(old, default), **PG.layout(old, raw)) + "\n"
        writes.append((PG.PLAN / f"{name}.json", raw, text))
        files.append({"file": f"plan/{name}.json", "made": n, "legacy": name in legacy})
    for name in twins:
        # A knob vector's golden: its twin's `before/` recording, only its
        # case name and source its own; it differs from the twin's default.
        base = name.removesuffix("_separate")
        default, twin = load("default", name), load("before", base)
        want = dict(twin, case=default["case"], **{"from": default["from"]})
        if PG.normal(default) != PG.normal(want):
            failed.append((name, "twin"))
            continue
        own = dict(load("default", base), case=default["case"])
        own["from"] = default["from"]
        if PG.normal(default) == PG.normal(own):
            failed.append((name, "twin equals its default"))
            continue
        raw = olds[base]
        text = json.dumps(default, **PG.layout(json.loads(raw), raw)) + "\n"
        writes.append((PG.PLAN / f"{name}.json", None, text))
        files.append({"file": f"plan/{name}.json", "made": 0, "new": True})
    for name, what in failed:
        print(json.dumps({"failed": name, "check": what}), flush=True)
    assert not failed, f"{len(failed)} goldens do not verify"
    for (golden, raw, text), f in zip(writes, files):
        if raw is not None:
            backup = out / "originals/plan" / golden.name
            backup.parent.mkdir(parents=True, exist_ok=True)
            with backup.open("x", encoding="utf-8") as stream:
                stream.write(raw)
        golden.write_text(text, encoding="utf-8")
        f["bytes"] = len(text.encode())
        f["sha256"] = hashlib.sha256(text.encode()).hexdigest()
        print(json.dumps(f), flush=True)
    made = sum(f["made"] for f in files)
    manifest = {"mutant": MUTANT, "select": SELECT, "made": made, "files": files}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"regolded {len(files)} goldens, {made} launches make operands", flush=True)


if __name__ == "__main__":
    main()
