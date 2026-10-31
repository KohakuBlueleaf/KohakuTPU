"""A compile_speed output as a table: each stage's share, its tail, its scaling.

python software/tests/perf/speed.py build/perf/speed.multimesh_v9t6.jsonl
python software/tests/perf/speed.py TRACE_DIR    # a trace's host cost a launch
"""

import json
import pathlib
import sys

import numpy as np
from traces import period

SKIP = ("path", "name", "l1_bytes", "l2_bytes", "packages", "error", "total")


def table(rows: list[dict]) -> dict:
    stages: dict = {}
    for r in rows:
        for k, v in r.items():
            if k not in SKIP:
                stages.setdefault(k, []).append((r["l2_bytes"], v))
    out = {}
    for k, pairs in stages.items():
        size, ms = np.array(pairs, dtype=float).T
        wide = ms > 0.01
        slope = 0.0
        if wide.sum() > 3 and np.ptp(np.log(size[wide])) > 0.5:
            slope = float(np.polyfit(np.log(size[wide]), np.log(ms[wide]), 1)[0])
        out[k] = dict(
            total_ms=float(ms.sum()),
            p50=float(np.median(ms)),
            p95=float(np.percentile(ms, 95)),
            max=float(ms.max()),
            exponent=slope,
        )
    return out


def host(trace: pathlib.Path) -> dict:
    """A trace's host cost a launch: everything before its warm steps, and the last
    of those, each as the gap before a launch and the submit itself, in ms."""
    rows = [json.loads(x) for x in (trace / "launches.jsonl").open(encoding="utf-8")]
    start, length = period([r["name"] for r in rows])
    warm = rows[start : start + length] if length else rows

    def part(chosen: list[dict]) -> dict:
        gaps = np.array([r.get("gap_ms", 0.0) for r in chosen] or [0.0])
        submits = np.array([r.get("submit_ms", 0.0) for r in chosen] or [0.0])
        return dict(
            launches=len(chosen),
            gap_total=float(gaps.sum()),
            gap_p50=float(np.median(gaps)),
            gap_max=float(gaps.max()),
            submit_total=float(submits.sum()),
            submit_p50=float(np.median(submits)),
            submit_max=float(submits.max()),
        )

    return dict(cold=part(rows[:start]), warm=part(warm))


def main():
    path = pathlib.Path(sys.argv[1])
    if path.is_dir():
        print(json.dumps(host(path), indent=1))
        return
    rows = [json.loads(x) for x in path.read_text("utf-8").splitlines()]
    done = [r for r in rows if "error" not in r]
    print(len(rows), "kernels,", len(rows) - len(done), "refused")
    for r in rows:
        if "error" in r:
            print("  refused", pathlib.Path(r["path"]).name, r["error"][:110])
    t = table(done)
    total = sum(v["total_ms"] for v in t.values())
    print(
        f"{'stage':16}{'share':>7}{'total s':>9}{'p50 ms':>9}{'p95 ms':>10}"
        f"{'max ms':>10}{'exp':>6}"
    )
    for k, v in sorted(t.items(), key=lambda kv: -kv[1]["total_ms"]):
        print(
            f"{k:16}{v['total_ms'] / total:7.1%}{v['total_ms'] / 1e3:9.2f}"
            f"{v['p50']:9.2f}{v['p95']:10.1f}{v['max']:10.1f}{v['exponent']:6.2f}"
        )
    print("slowest:")
    for r in sorted(done, key=lambda r: -r["total"])[:8]:
        worst = max((k for k in r if k not in SKIP), key=lambda k: r[k])
        print(
            f"  {r['total'] / 1e3:7.2f} s {pathlib.Path(r['path']).name[:40]:40} "
            f"L2 {r['l2_bytes']:6} B, worst {worst} {r[worst] / 1e3:.2f} s"
        )
    path.with_suffix(".table.json").write_text(json.dumps(t, indent=1), "utf-8")


if __name__ == "__main__":
    main()
