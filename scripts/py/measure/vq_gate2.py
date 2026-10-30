"""Run the lead-authorized Gate fixes 2 crates, retaining every output."""

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def main():
    out = ROOT / ".plan/vq/loop8/gate2"
    out.mkdir(parents=True, exist_ok=True)
    results = []
    for crate in ("ktpu-plan", "ktpu-kernels", "ktpu-l2"):
        proc = subprocess.run(
            ["cargo", "test", "-p", crate, "--features", "mutants"],
            cwd=ROOT / "software/rust",
            env=dict(os.environ, CARGO_TARGET_DIR="target/job"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=1800,
            check=False,
        )
        (out / f"{crate}.log").write_text(proc.stdout, encoding="utf-8")
        result = {
            "crate": crate,
            "exit": proc.returncode,
            "summaries": [
                line
                for line in proc.stdout.splitlines()
                if line.startswith("test result:")
            ],
        }
        results.append(result)
        print(json.dumps(result), flush=True)
        if proc.returncode:
            print(proc.stdout, flush=True)
    (out / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    assert all(r["exit"] == 0 for r in results), results


if __name__ == "__main__":
    main()
