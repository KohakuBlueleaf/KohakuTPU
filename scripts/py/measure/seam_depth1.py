"""Current-tree depth-one host costs, profiled and unprofiled separately."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "build/seam/depth1"


def category(row):
    file, fn = row["file"], row["function"]
    if "kohakutpu.compiler._rs.bind" in fn:
        return "binding/relocation"
    if "_hashlib.openssl_sha1" in fn or "_rs.package_admit" in fn:
        return "package build/submit/collect"
    if "/tinygrad/" in file:
        return "tinygrad scheduling/replay"
    if "queue/client.py" in file and fn in (
        "poll",
        "wait",
        "take",
        "_waited",
        "_idle",
        "state",
    ):
        return "completion poll"
    if fn in ("bind", "_bind", "snapshot", "resolve") or "builtins.bind" in fn:
        return "binding/relocation"
    if (
        "ktpugrad/" in file
        or "kernels/" in file
        or file.endswith(("forms.py", "rows.py", "tree.py", "generated.py"))
    ):
        return "ktpugrad planning/dispatch"
    if "queue/client.py" in file or file.endswith(("packages.py", "card.py")):
        return "package build/submit/collect"
    if file.endswith(("hseam_counting.py", "offline.py")) or "queue/double.py" in file:
        return "counting firmware/memory (not silicon)"
    if fn == "<built-in method time.sleep>":
        return "poll backoff sleep"
    return "other Python/runtime"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", default=str(ROOT / "build/seam/current-library"))
    parser.add_argument("--modes", nargs="*", default=["jit", "eager"])
    parser.add_argument("--label", default="baseline")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    summary = {}
    for mode in args.modes:
        for profile in (False, True):
            name = f"{args.label}-{mode}" + ("-profile" if profile else "")
            output = OUT / f"{name}.json"
            cmd = [
                sys.executable,
                str(ROOT / "scripts/py/measure/hseam.py"),
                "lenet-" + mode,
                "--counting",
                "--depth",
                "1",
                "--steps",
                "7",
                "--seconds",
                "180",
                "--library",
                args.library,
                "--prepared",
                os.environ.get("KOHAKUTPU_PREPARED_REPLAY", "0"),
                "--head-cache",
                "1",
                "--submit-batch",
                "1",
                "--cq-bulk",
                "0",
                "--backoff",
                "0",
                "--quiet",
                "--output",
                str(output),
            ]
            if profile:
                cmd.append("--profile")
            done = subprocess.run(
                cmd, capture_output=True, text=True, timeout=240, check=False
            )
            (OUT / f"{name}.log").write_text(done.stdout + done.stderr)
            if done.returncode:
                print(name, "FAILED", done.stderr[-2000:], flush=True)
                raise SystemExit(done.returncode)
            data = json.loads(output.read_text())
            rows = data.get("profile", [])
            costs = {}
            for row in rows:
                key = category(row)
                costs[key] = costs.get(key, 0) + row["exclusive_seconds"]
            summary[name] = {
                "steps": data["steps"],
                "jit": data["jit"],
                "exclusive_seconds": costs,
                "top": rows[:30],
            }
            print(
                name,
                "warm ms/launch",
                [round(s["ms_per_launch"], 4) for s in data["steps"][3:]],
                "launches",
                [s["launches"] for s in data["steps"][3:]],
                "calls",
                data["steps"][-1]["transport_calls"],
                flush=True,
            )
            if profile:
                print(
                    "exclusive ms",
                    {k: round(v * 1000, 2) for k, v in costs.items()},
                    flush=True,
                )
                print(
                    "top",
                    [
                        (
                            r["function"],
                            round(r["exclusive_seconds"] * 1000, 2),
                            r["calls"],
                        )
                        for r in rows[:12]
                    ],
                    flush=True,
                )
            (OUT / f"summary-{args.label}.json").write_text(
                json.dumps(summary, indent=2) + "\n"
            )


if __name__ == "__main__":
    main()
