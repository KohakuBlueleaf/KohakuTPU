"""Durable AST/package inventory from a warm library-only eager execution."""

import argparse
import json
import runpy
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from kohakutpu.application.ktpugrad import run as R
from kohakutpu.application.runtime import device as D
from kohakutpu.stack.buffer import Buffer

ROOT = Path(__file__).resolve().parents[3]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("model", choices=("lenet", "vit-tiny"))
    ap.add_argument("--mode", choices=("off", "on"), default="off")
    args = ap.parse_args()
    dev = D.attach(
        D.Device(
            "offline",
            board="multimesh_v9t6",
            library=ROOT / "build" / f"launch-library-{args.mode}",
            library_only=True,
            classes=False,
        )
    )
    prebuild = runpy.run_path(str(ROOT / "scripts/py/templates/prebuild.py"))
    rows, stack = [], []
    execute, submit = R.execute, dev.run
    asts = []

    def observed_execute(rt, plan, buffers):
        identity = len(asts)
        asts.append(
            {
                "index": identity,
                "kind": plan.kind,
                "shape": plan.out_shape,
                "expression": repr(plan.expr),
                "ast": repr(plan.ast),
                "origin": "unresolved_semantic_origin",
                "output_bytes": plan.out_n * plan.out_dtype.itemsize,
            }
        )
        stack.append(identity)
        try:
            return execute(rt, plan, buffers)
        finally:
            stack.pop()

    def observed_run(compiled, binds, kind="dsl", info=None):
        result = submit(compiled, binds, kind=kind, info=info)
        params = compiled.l1.params(compiled.name, "l1")
        sizes = {
            p: Buffer.of(t, dev.card.machine_json).nbytes
            for p, t in params
            if p in binds
        }
        rows.append(
            {
                "index": len(rows),
                "ast": stack[-1] if stack else None,
                "name": compiled.name,
                "kind": kind,
                "bound_bytes": sum(sizes.values()),
                "bindings": sizes,
                "cycles": None,
                "boundary": "runtime package inside scheduled AST",
            }
        )
        return result

    try:
        with (
            R.knobs(
                **{
                    k: args.mode == "on"
                    for k in (
                        "view_forms",
                        "elide_contiguous",
                        "single_package_ast",
                        "horizontal_fusion",
                    )
                }
            ),
            patch.object(R, "execute", observed_execute),
            patch.object(dev, "run", observed_run),
        ):
            steps = prebuild["MODELS"][args.model](
                8 if args.model == "lenet" else 1, False, 3
            )
        dest = ROOT / ".kt/launch-inventory"
        dest.mkdir(parents=True, exist_ok=True)
        payload = {
            "model": args.model,
            "mode": args.mode,
            "steps": steps,
            "asts": asts,
            "launches": rows,
            "kind_counts": dict(Counter(r["kind"] for r in rows)),
            "byte_definition": "sum of bound parameter extents, NOT measured DRAM traffic",
            "cycles": "offline does not execute; per-AST model cycles unavailable",
        }
        (dest / f"{args.model}-{args.mode}.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
        print(args.model, args.mode, len(asts), len(rows), payload["kind_counts"])
    finally:
        dev.close()


if __name__ == "__main__":
    main()
