"""Serial offline-library proofs or CPU accuracy measurements for the MNIST registry."""

import argparse
import json
import os
import pathlib
import subprocess
import sys

from examples.tinygrad.mnist_models import MODELS

ROOT = pathlib.Path(__file__).resolve().parents[3]


def execute(command, log):
    with log.open("w", encoding="utf-8") as stream:
        result = subprocess.run(
            command, stdout=stream, stderr=subprocess.STDOUT, check=False
        )
    if result.returncode:
        print(log.read_text(encoding="utf-8")[-12000:], flush=True)
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("offline", "cpu"))
    parser.add_argument(
        "--models", nargs="+", choices=list(MODELS), default=list(MODELS)
    )
    parser.add_argument("--batches", nargs="+", type=int, default=[32])
    parser.add_argument("--eval-batch", type=int)
    parser.add_argument(
        "--output", type=pathlib.Path, default=ROOT / "build/zoo/measurements"
    )
    args = parser.parse_args()
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    summary = []
    for name in args.models:
        for batch in args.batches:
            tag = f"{name}-b{batch}"
            folder = out / tag
            folder.mkdir(parents=True, exist_ok=True)
            row = dict(model=name, batch=batch)
            if args.mode == "cpu":
                command = [
                    sys.executable,
                    str(ROOT / "software/application/examples/tinygrad/mnist_mlp.py"),
                    "--model",
                    name,
                    "--batch",
                    str(batch),
                    "--eval-batch",
                    str(args.eval_batch or batch),
                    "--steps",
                    "1000",
                    "--reference",
                    "--schedule",
                    "cosine",
                    "--log",
                    "100",
                    "--report",
                    str(folder / "cpu.json"),
                ]
                row["cpu_exit"] = execute(command, folder / "cpu.log")
                if row["cpu_exit"] == 0:
                    row["cpu"] = json.loads((folder / "cpu.json").read_text())[
                        "curves"
                    ]["test"][-1]
            else:
                library = ROOT / "build/zoo" / tag
                common = [
                    sys.executable,
                    str(ROOT / "scripts/py/templates/prebuild.py"),
                    "--model",
                    name,
                    "--batch",
                    str(batch),
                    "--eval-batch",
                    str(args.eval_batch or batch),
                    "--classes",
                    "0",
                    "--library",
                    str(library),
                ]
                row["prebuild_exit"] = execute(
                    common + ["--mode", "prebuild", "--output", str(folder)],
                    folder / "prebuild.log",
                )
                if row["prebuild_exit"] == 0:
                    capture = json.loads((folder / "capture.json").read_text())
                    row["capture"] = {
                        "total": len(capture["launches"]),
                        "sources": len(capture["sources"]),
                        "phases": capture["phase_launches"],
                        "last_phases": capture["last_phase_launches"],
                    }
                    for schedule in ("constant", "cosine", "onecycle"):
                        run_out = folder / schedule
                        row[f"{schedule}_exit"] = execute(
                            common
                            + [
                                "--mode",
                                "run",
                                "--steps",
                                "6",
                                "--jit",
                                "--schedule",
                                schedule,
                                "--output",
                                str(run_out),
                            ],
                            folder / f"{schedule}.log",
                        )
                        if row[f"{schedule}_exit"] == 0:
                            run = json.loads((run_out / "run.json").read_text())
                            row[schedule] = {
                                k: run[k]
                                for k in (
                                    "compile_calls",
                                    "library_misses",
                                    "library_hits",
                                    "runs",
                                    "jit",
                                    "step_records",
                                )
                            }
                    if all(
                        row.get(f"{s}_exit") == 0
                        for s in ("constant", "cosine", "onecycle")
                    ):
                        runs = [
                            json.loads((folder / s / "run.json").read_text())
                            for s in ("constant", "cosine", "onecycle")
                        ]
                        row["same_launch_keys"] = (
                            runs[0]["launch_keys"]
                            == runs[1]["launch_keys"]
                            == runs[2]["launch_keys"]
                        )
                        if not row["same_launch_keys"]:
                            row["launch_key_difference"] = [
                                (i, a, b)
                                for i, (a, b) in enumerate(
                                    zip(runs[0]["launch_keys"], runs[1]["launch_keys"])
                                )
                                if a != b
                            ][:3]
                            row["launch_lengths"] = [
                                len(r["launch_keys"]) for r in runs
                            ]
            summary.append(row)
            (
                out
                / f"{args.mode}-{'-'.join(args.models)}-{'-'.join(map(str, args.batches))}.json"
            ).write_text(json.dumps(summary, indent=2), encoding="utf-8")
            print(
                json.dumps(
                    {
                        k: v
                        for k, v in row.items()
                        if k not in ("constant", "cosine", "onecycle")
                    }
                ),
                flush=True,
            )
    if args.mode == "offline":
        assert all(row.get("same_launch_keys", False) for row in summary), summary
    else:
        assert all(row["cpu_exit"] == 0 for row in summary), summary
    print(
        f"Measurements saved under {out}; CPU thread limit {os.environ.get('OMP_NUM_THREADS')}",
        flush=True,
    )


if __name__ == "__main__":
    main()
