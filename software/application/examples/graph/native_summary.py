"""Summarize native artifacts and subtract initialization-only model counters."""

import argparse
import json
import pickle
from pathlib import Path


def counters(path):
    result = {}
    for line in path.read_text().splitlines():
        fields = line.split()
        if fields and fields[0] in (
            "VRES",
            "V2MATH",
            "V2UNPACK",
            "V2PACK",
            "V2DMA",
            "V2DISPATCH",
        ):
            for name, value in zip(fields[2::2], fields[3::2], strict=True):
                result[fields[0] + "." + name] = int(value)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    root = args.directory
    artifact = pickle.loads((root / "block197-final.pkl").read_bytes())
    run = json.loads((root / "run8-final.json").read_text())
    init, full = counters(root / "run9-init.log"), counters(root / "run10-local.log")
    diff = {name: full[name] - value for name, value in init.items()}
    flops = (
        24 * artifact["tokens"] * artifact["width"] ** 2
        + 4 * artifact["tokens"] ** 2 * artifact["width"]
    )
    assert diff["VRES.lanes"] == 16 * diff["VRES.beats"]
    summary = dict(
        final={
            key: {
                k: v
                for k, v in value.items()
                if k not in ("output_hex", "phases", "host_submit_wait_seconds")
            }
            for key, value in run["records"].items()
        },
        builds={
            mode: dict(
                builds=v["builds"],
                hits=v["hits"],
                build_seconds=v["build_seconds"],
                package_bytes=sum(map(len, v["packets"])),
            )
            for mode, v in artifact["variants"].items()
        },
        warm_counters=diff,
        useful_matrix_flops=flops,
        target_28pct_cycles=flops / (artifact["peak_flops"] * 0.28),
        fixed_template_issue_floor_cycles=diff["VRES.beats"],
        fixed_template_issue_mfu_upper=flops
        / (artifact["peak_flops"] * diff["VRES.beats"]),
        cycle_reduction=1
        - run["records"]["local"]["cycles"] / run["records"]["off"]["cycles"],
        model_sessions_used=10,
        whole_model_bar_met=False,
        hardware_universal_wall_proven=False,
    )
    (root / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
