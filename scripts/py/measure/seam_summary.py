"""Summarize raw host timeline without equating host and firmware clocks."""

import argparse
import json
from collections import Counter
from pathlib import Path


def summarize(path, card=None):
    data = json.loads(path.read_text())
    print("FILE", path, data.get("status"), "JIT", data.get("jit"))
    for step in data["steps"]:
        i = step["step"]
        es = [e for e in data.get("timeline", ()) if e["step"] == i]
        submits = [e for e in es if e["phase"] == "submit_begin"]
        phases = Counter()
        for e in es:
            phases[e["phase"]] += (e["end_ns"] - e["start_ns"]) / 1e6
        print(
            "STEP",
            i,
            "seconds",
            step["seconds"],
            "calls",
            step["transport_calls"],
            "phase_ms",
            phases,
        )
        print(
            "GC_EVENTS",
            i,
            [
                (
                    e["launch"],
                    e.get("generation"),
                    round((e["end_ns"] - e["start_ns"]) / 1e6, 3),
                    e["parents"],
                )
                for e in es
                if e["phase"] == "gc" and e["end_ns"] - e["start_ns"] > 500000
            ],
        )
        if i not in (3, 4, len(data["steps"]) - 1):
            continue
        rows = [e for e in data["events"] if e["step"] == i]
        if card is not None:
            last = max(r["step"] for r in card["runs"])
            cs = [r for r in card["runs"] if r["step"] == last]
            print(
                "ALIGN",
                i,
                len(cs),
                len(rows),
                "name mismatches",
                sum(c["name"] != r["name"] for c, r in zip(cs, rows)),
            )
        else:
            cs = []
        starts = [e for e in es if e["phase"] == "step"]
        ends = [e for e in es if e["phase"] == "submit_end"]
        wanted = [0, 142, 170, 186, 222, 275, 291, 387, 532, 564]
        largest = sorted(
            range(1, len(submits)),
            key=lambda k: submits[k]["start_ns"] - ends[k - 1]["end_ns"],
            reverse=True,
        )[:10]
        for k in sorted(set(wanted + largest)):
            if k >= len(submits):
                continue
            lo = ends[k - 1]["end_ns"] if k else starts[0]["start_ns"]
            hi = submits[k]["start_ns"]
            inside = [e for e in es if e["end_ns"] > lo and e["start_ns"] < hi]
            traffic = Counter()
            for e in inside:
                if e["phase"] == "transport":
                    traffic[e["purpose"]] += e["nbytes"]
            ph = Counter()
            for e in inside:
                if e["phase"] not in ("transport", "submit_end"):
                    ph[e["phase"]] += (
                        max(0, min(hi, e["end_ns"]) - max(lo, e["start_ns"])) / 1e6
                    )
            print(
                "GAP",
                k,
                rows[k]["name"][:14],
                cs[k]["idle"] if cs else None,
                "host_ms",
                round((hi - lo) / 1e6, 4),
                "phases",
                ph,
                "bytes",
                traffic,
            )
    print(
        "TOP_PROFILE",
        [
            (r["function"], round(r["exclusive_seconds"], 4), r["calls"])
            for r in data.get("profile", ())[:25]
        ],
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", type=Path)
    ap.add_argument("--card", type=Path)
    args = ap.parse_args()
    summarize(args.input, json.loads(args.card.read_text()) if args.card else None)


if __name__ == "__main__":
    main()
