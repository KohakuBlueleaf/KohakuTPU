"""Every recorded trace on every machine it names, from nothing but the traces: timed,
then read. One JSON a run under `build/perf/`, the whole matrix in a table.

    python software/tests/perf/matrix.py
"""

import json
import pathlib
import subprocess
import sys
import time

import waste

ROOT = pathlib.Path(__file__).resolve().parents[3]
TRACES = ROOT / "build" / "perf" / "traces"
TIMED = ROOT / "software" / "target" / "stage" / "timed"
PARAMS = pathlib.Path(__file__).with_name("params.json")


def timed(trace: pathlib.Path, machine: str) -> None:
    cmd = [str(TIMED), "--machine", str(ROOT / "machines" / f"{machine}.json")]
    cmd += ["--params", str(PARAMS), "--card"]
    cmd += ["--out", str(trace / f"timed.{machine}.jsonl"), str(trace / "kernels")]
    subprocess.run(cmd, check=True, capture_output=True)


def main():
    t0 = time.monotonic()
    rows = []
    for path in sorted(TRACES.glob("*/meta.json")):
        trace = path.parent
        meta = json.loads(path.read_text("utf-8"))
        if not meta["launches"]:
            continue
        timed(trace, meta["board"])
        r = waste.report(trace, meta["board"])
        (trace / f"waste.{meta['board']}.json").write_text(
            json.dumps(r, indent=1), "utf-8"
        )
        rows.append((trace.name, r))
    print(
        f"{'trace':34}{'launches':>9}{'step M':>9}{'seams M':>9}{'math M':>8}"
        f"{'form M':>8}{'mg':>7}{'vc':>7}{'recover M':>11}"
    )
    for name, r in rows:
        f = r["floors"]
        print(
            f"{name:34}{r['step_launches']:9}{r['step_cycles'] / 1e6:9.2f}"
            f"{r['step_with_seams'] / 1e6:9.2f}{f['math'] / 1e6:8.2f}"
            f"{(f['with_form'] - f['compute']) / 1e6:8.2f}{r['mfu']['mg']:7.1%}"
            f"{r['mfu']['vc']:7.1%}{sum(r['recoverable'].values()) / 1e6:11.2f}"
        )
    print(f"{len(rows)} traces in {time.monotonic() - t0:.1f} s")


if __name__ == "__main__":
    sys.exit(main())
