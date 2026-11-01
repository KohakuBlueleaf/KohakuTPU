"""Print what a packed run says, before any figure uses it: totals reconcile or stop."""

import sys

from runs import by_kind, load


def main():
    run = load(sys.argv[1])
    steps = run.steps
    step = run.step
    print(run.name, "rows", len(run.rows), "start", run.start, "length", run.length)
    print("warm steps", len(steps))
    totals = sorted(sum(r["cycles"] for r in s) / 1e6 for s in steps)
    print(
        "step M: min", totals[0], "median", totals[len(totals) // 2], "max", totals[-1]
    )
    span = step[-1]["start"] + step[-1]["cycles"] - step[0]["start"]
    idle = sum(r.get("idle", 0) for r in step[1:])
    print("span M", span / 1e6, "cycles M", sum(r["cycles"] for r in step) / 1e6)
    print("idle M", idle / 1e6)
    for key in ("head", "copy", "bind", "steps", "barrier"):
        print(key, sum(r.get(key, 0) for r in step) / 1e6)
    print("kinds", {k: round(v / 1e6, 2) for k, v in by_kind(step, "cycles").items()})
    units: dict = {}
    for r in step:
        for u in r.get("units", ()):
            e = units.setdefault((u["kind"], u["x"], u["y"]), [0, 0, 0, 0])
            e[0] += u["busy_d"]
            e[1] += u["retired_d"]
            e[2] += u["compute_d"]
            e[3] += u["fill_d"]
    for k, v in units.items():
        print(k, [round(x / 1e6, 3) for x in v])
    print("host ms gap", sum(r.get("gap_ms", 0) for r in step))
    print("host ms submit", sum(r.get("submit_ms", 0) for r in step))
    print("spans", run.meta.get("spans"))


if __name__ == "__main__":
    main()
