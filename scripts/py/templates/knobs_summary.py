"""Assemble measured round-three data without estimating missing measurements."""

import argparse
import json
from pathlib import Path


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lib",
        type=Path,
        required=True,
        help="library measurement tree: reads round3/, writes measure.json",
    )
    parser.add_argument(
        "--capture", type=Path, required=True, help="the LeNet capture counts.py read"
    )
    args = parser.parse_args()
    lib = args.lib
    round3 = lib / "round3"
    static = load(round3 / "static.json")
    prebuild = load(round3 / "prebuild/summary.json")
    measurements = []
    for row, build in zip(
        static["measurements"], prebuild["measurements"], strict=True
    ):
        launches = row["launches"]
        measurements.append(
            {
                "policy": row["policy"],
                "cores": row["cores"],
                "static_command": static["command"],
                "prebuild_command": prebuild["command"],
                "templates": build["templates"],
                "prebuild_seconds": build["seconds"],
                "static_totals": {
                    key: sum(x["sizes"][key] for x in launches)
                    for key in ("steps", "payloads", "bytes", "buffer_bytes")
                },
                "padding_totals": {
                    key: sum(x["class_info"].get(key, 0) for x in launches)
                    for key in ("padded_rows", "padded_columns")
                },
                "launch_count": len(launches),
                "per_launch_evidence": str(round3 / "static.json"),
                "prebuild_evidence": build["ledger"],
                "activation": load(
                    round3 / f"activation-{row['policy']}-{row['cores']}.json"
                ),
            }
        )
    report = {
        "status": "measured policy sweep, count-path comparison, model prefix/activation and linked lanes; see scope limits",
        "command": "python scripts/py/templates/knobs_summary.py",
        "policies": measurements,
        "prefix": load(round3 / "prefix.json"),
        "count_model": load(round3 / "counts-model.json"),
        "lanes_link": load(round3 / "lanes-link.json"),
        "count_offline": [],
        "scope_limits": [
            "Policy sweep covers 459 non-lanes LeNet training capture launches; lanes comparison uses one multi-class relayout, not all 108.",
            "Activation cycles use four repeated 96-element ReLUs; prefix is the first four launches of batch-one LeNet training, not the full batch-eight step.",
            "Count-path model comparison uses one dispatch-count field; original captured register-control steps are refused by register lowering, not claimed supported.",
            "Prebuild includes differential derivation and byte proofs, not the still-pending confinement certification.",
        ],
    }
    for cores in ("all", "min"):
        report["count_offline"].append(
            {
                "command": f"python scripts/py/templates/counts.py {args.capture} {round3}/prebuild/pow2-{cores}/results.json {round3}/counts-{cores}.json",
                "cores": cores,
                "measurement": load(round3 / f"counts-{cores}.json"),
            }
        )
    (lib / "measure.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            [
                {
                    k: r[k]
                    for k in (
                        "policy",
                        "cores",
                        "templates",
                        "prebuild_seconds",
                        "static_totals",
                        "padding_totals",
                    )
                }
                for r in measurements
            ],
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
