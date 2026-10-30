"""Run bounded RELAY gate groups on the Linux host, retaining complete logs."""

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[3]
CRATES = ("ktpu-l0", "ktpu-l1", "ktpu-l2", "ktpu-machine", "ktpu-verify")


def run(out, name, command, cwd, seconds, extra=None):
    env = dict(os.environ, DEV="CPU", CARGO_TARGET_DIR="target/job")
    env.update(extra or {})
    start = time.monotonic()
    with (out / f"{name}.log").open("w", encoding="utf-8") as log:
        try:
            proc = subprocess.run(
                command,
                cwd=cwd,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=seconds,
                check=False,
            )
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            rc = 124
    result = {
        "name": name,
        "command": command,
        "code": rc,
        "seconds": time.monotonic() - start,
    }
    print(json.dumps(result), flush=True)
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("group", choices=("kernels", "rust", "golden", "vit"))
    ap.add_argument(
        "--output",
        type=pathlib.Path,
        default=ROOT / "build/relay/gates",
        help="directory receiving one log per gate and <group>.json",
    )
    args = ap.parse_args()
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    results = []
    sw = ROOT / "software"
    if args.group == "kernels":
        for test in sorted((sw / "application/tests/kernels").glob("test*.py")):
            results.append(
                run(
                    out,
                    test.stem,
                    [
                        sys.executable,
                        "-m",
                        "pytest",
                        str(test),
                        "-q",
                        "-n",
                        "4",
                        "-p",
                        "no:cacheprovider",
                        "--tb=short",
                    ],
                    sw,
                    115,
                )
            )
        results.append(
            run(
                out,
                "dsl",
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "application/tests/dsl",
                    "application/tests/test_l1_examples.py",
                    "-q",
                    "-n",
                    "4",
                    "-p",
                    "no:cacheprovider",
                    "--tb=short",
                ],
                sw,
                600,
            )
        )
    elif args.group == "vit":
        test = "application/tests/kernels/test_vit_rows.py"
        for op in ("softmax", "layernorm", "sum", "max", "prod", "elementwise"):
            for width in (128, 197):
                results.append(
                    run(
                        out,
                        f"vit-{op}-{width}",
                        [
                            sys.executable,
                            "-m",
                            "pytest",
                            test,
                            "-k",
                            f"logical_rows and {op}-{width}",
                            "-q",
                            "-n",
                            "4",
                            "-p",
                            "no:cacheprovider",
                            "--tb=short",
                        ],
                        sw,
                        115,
                    )
                )
        results.append(
            run(
                out,
                "vit-other",
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    test,
                    "-k",
                    "not logical_rows",
                    "-q",
                    "-n",
                    "4",
                    "-p",
                    "no:cacheprovider",
                    "--tb=short",
                ],
                sw,
                115,
            )
        )
    elif args.group == "rust":
        for crate in CRATES:
            results.append(
                run(
                    out,
                    crate,
                    ["cargo", "test", "-p", crate, "--features", "mutants"],
                    sw / "rust",
                    1800,
                )
            )
            results.append(
                run(
                    out,
                    crate + "-fmt",
                    ["cargo", "fmt", "-p", crate, "--", "--check"],
                    sw / "rust",
                    120,
                )
            )
            results.append(
                run(
                    out,
                    crate + "-clippy",
                    [
                        "cargo",
                        "clippy",
                        "-p",
                        crate,
                        "--features",
                        "mutants",
                        "--",
                        "-D",
                        "warnings",
                    ],
                    sw / "rust",
                    1800,
                )
            )
    else:
        for shard in range(16):
            results.append(
                run(
                    out,
                    f"golden-{shard}",
                    [
                        "cargo",
                        "test",
                        "-p",
                        "ktpu",
                        "--features",
                        "mutants",
                        "--test",
                        "golden",
                        "the_stack_agrees_with_the_python_goldens",
                        "--",
                        "--exact",
                    ],
                    sw / "rust",
                    1800,
                    {"KTPU_GOLDEN_SHARD": f"{shard}/16"},
                )
            )
    (out / f"{args.group}.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8"
    )
    return int(any(r["code"] for r in results))


if __name__ == "__main__":
    sys.exit(main())
