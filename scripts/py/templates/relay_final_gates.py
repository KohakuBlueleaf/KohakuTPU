"""Final host gate slices, at most 115 seconds per pytest invocation."""

import json
import os
import subprocess
import sys

from relay_gates import OUT, ROOT, run

GROUPS = {
    "tier0": [
        "tests/spec/test_spec.py",
        "tests/spec/test_routing.py",
        "tests/spec/test_tier2_safety.py",
        "tests/spec/test_switch.py",
        "tests/spec/test_sru_oracles.py",
    ],
    "application": [
        "application/tests/runtime",
        "application/tests/api",
        "application/tests/ktpugrad",
        "application/tests/kernels",
    ],
}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    group = sys.argv[1]
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *GROUPS[group],
            "--collect-only",
            "-q",
            "-n",
            "0",
            "-p",
            "no:cacheprovider",
        ],
        cwd=ROOT / "software",
        env=dict(os.environ, DEV="CPU"),
        capture_output=True,
        text=True,
        check=False,
        timeout=115,
    )
    ids = [
        s
        for s in proc.stdout.splitlines()
        if "::" in s and s.startswith(("tests/", "application/"))
    ]
    (OUT / f"final-{group}-collection.log").write_text(
        proc.stdout + proc.stderr, encoding="utf-8"
    )
    results = []
    size = 100 if group == "tier0" else 16
    for start in range(0, len(ids), size):
        chosen = ids[start : start + size]
        result = run(
            f"final-{group}-{start}",
            [
                sys.executable,
                "-m",
                "pytest",
                *chosen,
                "-q",
                "-n",
                "4",
                "-p",
                "no:cacheprovider",
                "--tb=short",
            ],
            ROOT / "software",
            115,
        )
        result.pop("command")
        results.append(result)
    (OUT / f"final-{group}.json").write_text(
        json.dumps(
            dict(collection=proc.returncode, tests=len(ids), slices=results), indent=2
        ),
        encoding="utf-8",
    )
    return int(proc.returncode != 0 or any(r["code"] for r in results))


if __name__ == "__main__":
    sys.exit(main())
