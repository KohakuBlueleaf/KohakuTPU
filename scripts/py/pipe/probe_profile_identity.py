"""Offline versus enumerated runtime machine/profile identity; no transport opens."""

import argparse
import json
from pathlib import Path

from kohakutpu.application.runtime import costs
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.stack import rs


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--costs", type=Path, required=True, help="padding-costs JSON")
    ap.add_argument(
        "--capture", type=Path, required=True, help="capture JSON with `sources`"
    )
    args = ap.parse_args()
    board = "multimesh_v9t6"
    offline = OfflineCard(board)
    found = {k: offline.machine.meshes[0].units.get(k, ()) for k in ("MG", "VC", "SR")}
    runtime = machine_of(board, nodes=1, units=found)
    profiles = costs._profile(str(args.costs))
    for tag, machine in [("offline", offline.machine), ("enumerated", runtime)]:
        print(
            tag,
            "board",
            machine.board,
            "units",
            machine.meshes[0].units,
            "schedule",
            profiles[0],
        )
        source = json.loads(args.capture.read_text())["sources"][
            "b5722073f594be01fc78380a4440ddd1657ae0e33b1c01ea70e50162a1646653"
        ]["source"]
        print(rs._rs.library_key(source, rs.machine_json(machine)))
        for (kind, b), records in profiles[1].items():
            if b == board:
                for recipe, _ in records.values():
                    print(kind, recipe, costs.choose(kind, recipe, machine.board))


if __name__ == "__main__":
    main()
