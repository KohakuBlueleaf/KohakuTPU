"""What a step spends cycles on, one label a cycle, and what it could drop: each
detector reads a trace's data flow and names the cycles its finding would recover.

    python software/tests/perf/waste.py TRACE_DIR --machine multimesh_v9t6 --out OUT.json
"""

import argparse
import collections
import json
import pathlib

from traces import FORM_KINDS, Launch, events, load, planned, steady_summary, step

ROOT = pathlib.Path(__file__).resolve().parents[3]
PARAMS = pathlib.Path(__file__).with_name("params.json")


def value(key: str) -> float:
    entry = json.loads(PARAMS.read_text("utf-8"))[key]
    return entry.get("card", entry["value"])


def floors(launches: list[Launch]) -> dict:
    """Serial floors of a step: math alone, then with the form work the stack added."""
    useful = compute = form = 0.0
    for x in launches:
        f = x.timed["floor"]
        fl = max(f["mg"], f["vc"], f["read"], f["write"])
        form += fl if x.kind in FORM_KINDS else 0.0
        compute += fl if x.kind not in FORM_KINDS else 0.0
        useful += max(f["mg"], f["vc"]) if x.kind not in FORM_KINDS else 0.0
    return {"math": useful, "compute": compute, "with_form": compute + form}


def labels(launches: list[Launch]) -> dict:
    """Every cycle of the step under one label, serial launches."""
    front, gap = value("launch_front"), value("launch_gap")
    table: dict[str, float] = collections.defaultdict(float)
    for x in launches:
        table["seam.front"] += front
        table["seam.gap"] += gap
        table[f"{x.kind}.run"] += x.cycles
    return dict(table)


def report_kinds(launches: list[Launch]) -> dict:
    kinds: dict = {}
    for x in launches:
        k = kinds.setdefault(x.kind, collections.Counter())
        k["launches"] += 1
        k["cycles"] += x.cycles
        k["stalled"] += x.timed.get("stalled", 0.0)
        for name, v in x.timed.get("work", {}).items():
            k[name] += v
    return {k: dict(v) for k, v in kinds.items()}


class Flow:
    """Each live address: its form, what produced it, whether anything read it, and
    the value's lineage, which a form change keeps and a computation starts."""

    def __init__(self):
        self.live: dict[int, dict] = {}
        self.fresh = 0

    def forms_of(self, lineage: int) -> dict:
        return {v["form"]: at for at, v in self.live.items() if v["lineage"] == lineage}

    def lineage(self, at: int) -> int:
        if at not in self.live:
            self.fresh += 1
            self.live[at] = {
                "form": "?",
                "lineage": self.fresh,
                "by": None,
                "read": True,
                "kind": "host",
            }
        return self.live[at]["lineage"]


def detect(launches: list[Launch]) -> list[dict]:
    """Findings over the whole trace; a finding's `cycles` are its launch's."""
    flow = Flow()
    indexed = {x.index: x for x in launches}
    found = []

    def finding(kind: str, x: Launch, why: str, cycles: float | None = None):
        found.append(
            {
                "detector": kind,
                "launch": x.index,
                "name": x.name,
                "kernel_kind": x.kind,
                "cycles": x.cycles if cycles is None else cycles,
                "why": why,
            }
        )

    last_compute = None
    for x in launches:
        form_change = x.kind in FORM_KINDS
        inputs = [flow.lineage(at) for at in x.reads]
        for at in x.reads:
            flow.live[at]["read"] = True
        for at, p in x.writes.items():
            old = flow.live.get(at)
            if old is not None and not old["read"] and old["by"] is not None:
                finding(
                    "written_unread",
                    indexed[old["by"]],
                    f"its {old['form']} output at {at:#x} is overwritten by "
                    f"launch {x.index} before anything reads it",
                )
            if form_change and len(inputs) == 1:
                lineage = inputs[0]
                have = flow.forms_of(lineage)
                if p.dtype in have and have[p.dtype] != at:
                    finding(
                        "form_held",
                        x,
                        f"makes {p.dtype} of a value already live as {p.dtype} at "
                        f"{have[p.dtype]:#x}",
                    )
            else:
                flow.fresh += 1
                lineage = flow.fresh
            flow.live[at] = {
                "form": p.dtype,
                "lineage": lineage,
                "by": x.index,
                "read": False,
                "kind": x.kind,
            }
        if not form_change:
            if last_compute is not None and last_compute == x.index - 1:
                a, b = indexed[x.index - 1], x
                if set(a.writes) & set(b.reads):
                    finding(
                        "launch_seam",
                        b,
                        f"reads what launch {a.index} just wrote: one launch would "
                        "drop this launch's front and gap",
                        value("launch_front") + value("launch_gap"),
                    )
            last_compute = x.index
    return found


def chains(launches: list[Launch]) -> list[dict]:
    """Form work standing between two computations: what a producer writing its
    consumer's form would drop."""
    out, run = [], []
    for x in launches:
        if x.kind in FORM_KINDS:
            run.append(x)
            continue
        if run:
            out.append(
                {
                    "before": x.index,
                    "kinds": [r.kind for r in run],
                    "cycles": sum(r.cycles for r in run),
                }
            )
        run = []
    return out


def median(values: list[float]) -> float:
    """The middle of `values` (the upper of two), 0 when there are none."""
    return sorted(values)[len(values) // 2] if values else 0.0


def report(trace: pathlib.Path, machine: str) -> dict:
    launches = load(trace, machine)
    meta = json.loads((trace / "meta.json").read_text("utf-8"))
    selection = None
    if meta.get("schema", 0) >= 2:
        steady = steady_summary(trace, meta)
        groups = steady["groups"]
        if not groups:
            raise ValueError("no replayed/steady input to model")
        group = next((g for g in ("input", "train") if g in groups), next(iter(groups)))
        selection = {"group": group, "input": groups[group]["representative_input"]}
        warm = [x for x in launches if x.input == selection["input"]]
    else:
        warm = step(launches, planned(meta["args"]))
    if not warm:
        raise ValueError("selected input has no actual compiled launch")
    found = detect(warm if meta.get("schema", 0) >= 2 else launches)
    first, last = warm[0].index, warm[-1].index
    in_step = [f for f in found if first <= f["launch"] <= last]
    by = collections.Counter()
    for f in in_step:
        by[f["detector"]] += f["cycles"]
    total = sum(x.cycles for x in warm)
    table = labels(warm)
    busy = sum(table.values())
    replays = [e for e in events(trace) if e.get("phase") == "replay"]
    return {
        "host_ms": {
            "between": sum(x.gap_ms for x in warm),
            "submit": sum(x.submit_ms for x in warm),
            "replays": len(replays),
            "replay_between": median([e["gap_ms"] for e in replays]),
            "replay_publish": median([e["submit_ms"] for e in replays]),
        },
        "mfu": {
            "mg": sum(x.timed["floor"]["mg"] for x in warm) / busy,
            "vc": sum(x.timed["floor"]["vc"] for x in warm) / busy,
            "read_bus": sum(x.timed["floor"]["read"] for x in warm) / busy,
            "write_bus": sum(x.timed["floor"]["write"] for x in warm) / busy,
        },
        "selection": selection,
        "dataflow_unavailable": [x.name for x in warm if not x.dataflow_available],
        "detector_scope": "inter-launch dataflow where L2 is retained; combined graph internals are opaque",
        "evidence": "timed model of actual selected launched programs, not measured savings",
        "trace": trace.name,
        "machine": machine,
        "launches": len(launches),
        "step_launches": len(warm),
        "step_cycles": total,
        "step_with_seams": sum(table.values()),
        "floors": floors(warm),
        "kinds": report_kinds(warm),
        "labels": table,
        "recoverable": dict(by),
        "findings": in_step,
        "chains": chains(warm),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("trace", type=pathlib.Path)
    ap.add_argument("--machine", default="multimesh_v9t6")
    ap.add_argument("--out", type=pathlib.Path)
    a = ap.parse_args()
    r = report(a.trace, a.machine)
    if a.out:
        a.out.write_text(json.dumps(r, indent=1), "utf-8")
    print(r["trace"], r["step_launches"], "launches a step")
    print(
        f"  step {r['step_cycles'] / 1e6:.2f} M, with seams "
        f"{r['step_with_seams'] / 1e6:.2f} M, floors",
        r["floors"],
    )
    for k, v in sorted(r["kinds"].items(), key=lambda kv: -kv[1]["cycles"]):
        print(f"  {k:10} {v['launches']:5.0f} {v['cycles'] / 1e6:8.2f} M")
    print("  recoverable", {k: round(v / 1e6, 2) for k, v in r["recoverable"].items()})
    print(
        "  mfu", {k: round(v, 3) for k, v in r["mfu"].items()}, "host ms", r["host_ms"]
    )


if __name__ == "__main__":
    main()
