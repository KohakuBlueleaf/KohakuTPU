"""Audit/export quant launch schedules, then run the specifically authorized planner gate.

Uses the existing plan_regold example. Old fixtures are retained under originals;
reviewed replacements and a manifest are exported for the lead's dataset upload.
"""

import argparse
import ast
import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RUST = ROOT / "software/rust"


def run(command, output, name, expected=0):
    result = subprocess.run(
        command,
        cwd=RUST,
        check=False,
        env=dict(os.environ, CARGO_TARGET_DIR="target/job"),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    (output / f"{name}.log").write_text(result.stdout, encoding="utf-8")
    print(result.stdout, flush=True)
    assert result.returncode == expected, (name, result.returncode)
    return result.stdout


def launches(node):
    if isinstance(node, dict):
        if (
            "launch" in node
            and node["launch"].get("call", {}).get("gen") == "matmul.quant_kernel"
        ):
            yield node["launch"]
        for value in node.values():
            yield from launches(value)
    elif isinstance(node, list):
        for value in node:
            yield from launches(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    # Extract defaults from the actual Python launch producer, not another table.
    tree = ast.parse(
        (
            ROOT / "software/application/kohakutpu/application/runtime/generated.py"
        ).read_text()
    )
    producer = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "launch"
    )
    schedule_expr = next(
        n.value
        for n in ast.walk(producer)
        if isinstance(n, ast.Assign)
        and any(
            isinstance(t, ast.Subscript)
            and isinstance(t.slice, ast.Constant)
            and t.slice.value == "schedule"
            for t in n.targets
        )
    )
    schedule = {
        name: int(default)
        for name, _, default in ast.literal_eval(schedule_expr.generators[0].iter)
    }
    before = run(
        ["cargo", "test", "-p", "ktpu-plan", "--test", "run"], output, "before", 101
    )
    assert ".kw: 8 vs 0 items" in before
    run(
        [
            "cargo",
            "run",
            "-p",
            "ktpu-kernels",
            "--example",
            "plan_regold",
            "--",
            "--quant-schedule",
            "--output",
            str(output / "plan"),
        ],
        output,
        "regold",
    )
    files = []
    for path in sorted((output / "plan").glob("*.json")):
        original = RUST / "golden/plan" / path.name
        old = json.loads(original.read_text())
        new = json.loads(path.read_text())
        changes = 0
        for a, b in zip(launches(old), launches(new), strict=True):
            if a == b:
                continue
            expected = json.loads(json.dumps(a))
            expected["name"] = b["name"]
            expected["graph"]["name"] = b["graph"]["name"]
            for node in expected["graph"]["body"]:
                if node.get("op") == "quant":
                    assert all(k == "finite" for k, _ in node["kw"])
                    node["kw"].extend([[k, {"int": v}] for k, v in schedule.items()])
            assert expected == b, (path.name, "unexpected graph or launch change")
            a.clear()
            a.update(expected)
            changes += 1
        assert old == new, (path.name, "non-quant fixture changes")
        backup = output / "originals" / path.name
        backup.parent.mkdir(parents=True, exist_ok=True)
        with backup.open("xb") as stream:
            stream.write(original.read_bytes())
        shutil.copyfile(path, original)
        files.append({"file": "plan/" + path.name, "launches": changes})
    assert files
    (output / "manifest.json").write_text(
        json.dumps({"schedule": schedule, "files": files}, indent=2) + "\n",
        encoding="utf-8",
    )
    run(
        ["cargo", "test", "-p", "ktpu-plan", "--test", "run", "--", "--nocapture"],
        output,
        "after",
    )
    print(json.dumps(files, indent=2), flush=True)


if __name__ == "__main__":
    main()
