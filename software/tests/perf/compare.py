"""The model against ground truth: anchors on a Verilated card, or a recorded card run
launch by launch. Prints each row's error and returns the worst.

    python software/tests/perf/compare.py anchors TRUTH.jsonl MODEL.jsonl
    python software/tests/perf/compare.py card TRACE_DIR --machine multimesh_v9t6
    python software/tests/perf/compare.py three TRACE_DIR TRUTH.jsonl
"""

import argparse
import collections
import json
import pathlib

import numpy as np

SEAM = ("head", "copy", "bind", "barrier")


def jsonl(path: pathlib.Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x]


def anchors(truth: pathlib.Path, model: pathlib.Path, verbose: bool = True) -> dict:
    """`steps` of a warm run is what the model's kernel covers; the rest is the seam."""
    rows = {r["name"]: r for r in jsonl(model)}
    out = {}
    for t in jsonl(truth):
        if "error" in t:
            print("  no truth", t["name"], t["error"][:120])
            continue
        m = rows.get(t["name"], {})
        if "cycles" not in m:
            print("  no model", t["name"], m.get("error"))
            continue
        steps = t["phases"]["steps"]
        out[t["name"]] = dict(
            truth=steps,
            model=m["cycles"],
            error=m["cycles"] / steps - 1.0,
            seam=sum(t["phases"].get(k, 0) for k in SEAM),
        )
        if verbose:
            o = out[t["name"]]
            print(
                f"  {t['name']:16} truth {steps:8} model {m['cycles']:10.0f} "
                f"{o['error']:+7.1%}  seam {o['seam']}"
            )
    return out


def card(trace: pathlib.Path, machine: str) -> dict:
    """Each launch's `steps` on the card against the model's kernel, by kind."""
    rows = {r["name"]: r for r in jsonl(trace / f"timed.{machine}.jsonl")}
    runs = jsonl(trace / "card.jsonl")
    kinds: dict = collections.defaultdict(lambda: collections.Counter())
    errors, seen = [], 0
    for r in runs:
        m = rows.get(r["name"])
        if m is None or "steps" not in r:
            continue
        seen += 1
        k = kinds[r["kind"]]
        k["launches"] += 1
        k["truth"] += r["steps"]
        k["model"] += m["cycles"]
        k["seam"] += sum(r.get(s, 0) for s in SEAM)
        k["idle"] += r.get("idle", 0)
        errors.append(abs(np.log(max(m["cycles"], 1.0) / max(r["steps"], 1.0))))
    print(
        f"  {seen} of {len(runs)} runs matched; median |error| "
        f"{np.exp(np.median(errors)) - 1:.1%}, p90 {np.exp(np.percentile(errors, 90)) - 1:.1%}"
    )
    for k, v in sorted(kinds.items(), key=lambda kv: -kv[1]["truth"]):
        print(
            f"  {k:10} {v['launches']:6.0f} truth {v['truth'] / 1e6:9.3f} M "
            f"model {v['model'] / 1e6:9.3f} M {v['model'] / max(v['truth'], 1) - 1:+7.1%} "
            f"seam {v['seam'] / 1e6:7.3f} M idle {v['idle'] / 1e6:8.3f} M"
        )
    return {k: dict(v) for k, v in kinds.items()}


def three(trace: pathlib.Path, truth: pathlib.Path, machine: str) -> dict:
    """Recorded kernels read three ways: the model, Verilator, the card."""
    rows = {r["name"]: r for r in jsonl(trace / f"timed.{machine}.jsonl")}
    cards: dict = collections.defaultdict(list)
    for r in jsonl(trace / "card.jsonl"):
        cards[r["name"]].append(r)
    kinds: dict = collections.defaultdict(list)
    for t in jsonl(truth):
        if "error" in t:
            print("  no truth", t["name"][:14], t["error"][:140])
            continue
        c = cards[t["name"]]
        steps = float(np.median([r["steps"] for r in c]))
        v, m = t["phases"]["steps"], rows[t["name"]]["cycles"]
        kinds[c[0]["kind"]].append((m, v, steps))
        print(
            f"  {c[0]['kind']:8} {t['name'][:14]} model {m:9.0f} "
            f"verilator {v:9.0f} card {steps:9.0f} "
            f"v/m {v / m:5.2f} card/v {steps / v:5.2f}"
        )
    out = {}
    for k, v in sorted(kinds.items()):
        a = np.array(v)
        out[k] = dict(
            n=len(v),
            verilator_over_model=float(np.median(a[:, 1] / a[:, 0])),
            card_over_verilator=float(np.median(a[:, 2] / a[:, 1])),
        )
        print(
            f"  {k:8} n {len(v):3}  verilator/model "
            f"{out[k]['verilator_over_model']:5.2f}  card/verilator "
            f"{out[k]['card_over_verilator']:5.2f}"
        )
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=("anchors", "card", "three"))
    ap.add_argument("paths", nargs="+", type=pathlib.Path)
    ap.add_argument("--machine", default="multimesh_v9t6")
    a = ap.parse_args()
    if a.mode == "anchors":
        got = anchors(a.paths[0], a.paths[1])
        print("worst", max(abs(v["error"]) for v in got.values()))
    elif a.mode == "three":
        three(a.paths[0], a.paths[1], a.machine)
    else:
        card(a.paths[0], a.machine)


if __name__ == "__main__":
    main()
