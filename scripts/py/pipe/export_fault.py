"""Snapshot real runtime table bindings at the first fault site, then compile libraries."""

import argparse
import json
import runpy
from pathlib import Path

from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import rs

ROOT = Path(__file__).resolve().parents[3]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", default="multimesh_v9t6")
    ap.add_argument(
        "--work", type=Path, required=True, help="pipe work tree (writes loop6/)"
    )
    args = ap.parse_args()
    base = args.work / "loop6"
    base.mkdir(parents=True, exist_ok=True)
    dev = D.attach(D.Device(kind="offline", board=args.board, classes=False))
    dev.machine = machine_of(args.board, nodes=1, packing_backend="vector_mx7")
    snapshots = []

    class Events(list):
        def append(self, event):
            super().append(event)
            if len(snapshots) >= 5:
                return
            snapshot = dict(
                event,
                data={
                    p["name"]: dev.read(p["address"], p["nbytes"]).hex()
                    for p in event["ports"]
                },
            )
            snapshots.append(snapshot)

    prebuild = runpy.run_path(str(ROOT / "scripts/py/templates/prebuild.py"))
    doc = prebuild["capture"](dev, 8, edge_events=Events())
    for event in snapshots:
        event["source"] = doc["sources"][event["key"]]["source"]
    (base / f"runtime-prefix-{args.board}.json").write_text(json.dumps(snapshots))
    for event in snapshots:
        result = rs._rs.library_prebuild(
            str(base / "library"), event["source"], rs.machine_json(dev.machine)
        )
        print(
            args.board, event["index"], event["key"][:10], str(result)[:180], flush=True
        )


if __name__ == "__main__":
    main()
