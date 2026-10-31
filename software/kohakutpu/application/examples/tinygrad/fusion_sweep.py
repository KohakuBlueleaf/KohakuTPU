"""Fresh-process offline fusion ablations, with per-case timeouts and failure records."""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

LEVELS = ("0", "elementwise", "bounded", "1")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase", choices=("census", "compile", "reference"), default="census"
    )
    parser.add_argument(
        "--workload", choices=("probe", "block", "vit", "lenet"), default="probe"
    )
    parser.add_argument(
        "--full", action="store_true", help="ViT tiny/base b1/b8, or LeNet b8"
    )
    parser.add_argument("--board", default="multimesh_v9t6")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--library", type=Path)
    parser.add_argument("--library-only", action="store_true")
    parser.add_argument("--record", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    shapes = (
        [(w, b) for w in (192, 768) for b in (1, 8)]
        if args.full and args.workload in ("block", "vit")
        else [(192, 8 if args.workload == "lenet" else 1)]
    )
    records = []
    for width, batch in shapes:
        for level in LEVELS:
            for eager in ((False,) if args.phase == "reference" else (False, True)):
                name = f"{args.workload}-w{width}-b{batch}-{level}-{'eager' if eager else 'lazy'}"
                output = args.output / f"{name}.json"
                command = [
                    sys.executable,
                    "-m",
                    "examples.tinygrad.fusion_measure",
                    "--phase",
                    args.phase,
                    "--workload",
                    args.workload,
                    "--level",
                    level,
                    "--width",
                    str(width),
                    "--batch",
                    str(batch),
                    "--board",
                    args.board,
                    "--output",
                    str(output),
                ]
                if args.record:
                    command += ["--record", str(args.output / f"{name}-record.json")]
                if eager:
                    command.append("--realize-ops")
                if args.library:
                    command += ["--library", str(args.library / name)]
                if args.library_only:
                    command.append("--library-only")
                started = time.perf_counter()
                try:
                    process = subprocess.run(
                        command,
                        capture_output=True,
                        text=True,
                        timeout=args.timeout,
                        check=False,
                    )
                    record = dict(
                        case=name,
                        returncode=process.returncode,
                        seconds=time.perf_counter() - started,
                    )
                    if process.returncode:
                        record["error"] = process.stderr[-8000:]
                    else:
                        result = json.loads(output.read_text(encoding="utf-8"))
                        record.update(
                            {
                                k: v
                                for k, v in result.items()
                                if k not in ("kernels", "values")
                            }
                        )
                except subprocess.TimeoutExpired as error:
                    record = dict(
                        case=name,
                        returncode="timeout",
                        seconds=time.perf_counter() - started,
                        error=(error.stderr or b"")[-8000:].decode(
                            "utf-8", errors="replace"
                        ),
                    )
                records.append(record)
                (args.output / "summary.json").write_text(
                    json.dumps(records, indent=2), encoding="utf-8"
                )
                print(json.dumps(record), flush=True)
    print(
        f"{sum(r['returncode'] == 0 for r in records)}/{len(records)} cases completed",
        flush=True,
    )


if __name__ == "__main__":
    main()
