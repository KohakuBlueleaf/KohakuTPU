"""Inventory named-field certificates for saved LIB2 artifacts; not a value proof."""

import argparse
import json
from pathlib import Path

from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.stack import rs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--artifacts",
        type=Path,
        required=True,
        help="LIB2 artifact tree: nonlanes/experimental-templates, lanes-artifacts, "
        "vlt-lanes/lanes-artifacts",
    )
    args = parser.parse_args()
    lib2 = args.artifacts
    result = {"artifacts": [], "refused": []}
    groups = [
        (
            "multimesh_v9t6",
            [
                (p.with_suffix(".bin"), p)
                for p in (lib2 / "nonlanes/experimental-templates").glob("*.json")
            ],
        ),
        (
            "multimesh_v9t6",
            [
                (p.parent / "package.bin", p)
                for p in (lib2 / "lanes-artifacts").glob("*/table.json")
            ],
        ),
        (
            "vlt_v9t6s_1n",
            [
                (p.parent / "package.bin", p)
                for p in (lib2 / "vlt-lanes/lanes-artifacts").glob("*/table.json")
            ],
        ),
    ]
    for board, files in groups:
        machine = rs.machine_json(OfflineCard(board).machine)
        for binary, table in sorted(files):
            try:
                cert = json.loads(
                    rs._rs.template_certificate(
                        binary.read_bytes(), table.read_text(encoding="utf-8"), machine
                    )
                )
                result["artifacts"].append(
                    {"table": str(table), "board": board, "certificate": cert}
                )
            except ValueError as error:
                result["refused"].append(
                    {"table": str(table), "board": board, "error": str(error)}
                )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {"classified": len(result["artifacts"]), "refused": len(result["refused"])}
        )
    )


if __name__ == "__main__":
    main()
