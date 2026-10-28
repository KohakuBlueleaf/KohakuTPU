"""Run bounded offline compile shards, preserving every pytest summary."""

import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
SOFTWARE = ROOT / "software"


def main():
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        str(SOFTWARE / part)
        for part in (
            "language",
            "compiler",
            "application",
            "driver",
            "simulation",
            "firmware",
        )
    )
    env["KTPU_RS"] = "1"
    env["KOHAKUTPU_DEVICE"] = "offline"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "application/tests/ktpugrad/test_compile.py",
        "--collect-only",
        "-q",
        "-n",
        "8",
        "-p",
        "no:cacheprovider",
        "-k",
        "test_compiles",
    ]
    collected = subprocess.run(
        command, cwd=SOFTWARE, env=env, capture_output=True, text=True, check=True
    )
    nodes = [
        line
        for line in collected.stdout.splitlines()
        if line.startswith("application/") and "::test_compiles[" in line
    ]
    if not nodes:
        raise RuntimeError(collected.stdout + collected.stderr)
    first = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    for start in range(first * 16, len(nodes), 16):
        batch = nodes[start : start + 16]
        print(f"COMPILE SHARD {start//16}: {len(batch)} cases", flush=True)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                *batch,
                "-q",
                "-n",
                "8",
                "-p",
                "no:cacheprovider",
                "--tb=short",
            ],
            cwd=SOFTWARE,
            env=env,
            check=True,
        )
    print(f"Offline compile shards {first}..{(len(nodes)-1)//16} passed", flush=True)


if __name__ == "__main__":
    main()
