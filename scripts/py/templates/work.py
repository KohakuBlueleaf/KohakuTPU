"""Analytical submission counts of class node loops, not a timing/value model."""

import argparse
import json
import math
import pathlib
from collections import Counter

from derive import decode


def ceildiv(n, d):
    return -(-n // d)


def work(family, a, info):
    result = {}
    if family in ("walk", "copy"):
        words = (
            math.prod(n for n, _ in a["levels"])
            if family == "walk"
            else ceildiv(a["nbytes"], 32)
        )
        ranks = len(a["levels"]) + len(a["dlevels"]) if family == "walk" else 2
        result.update(
            mover_submissions=words,
            address_alu=words * (4 * ranks + 6),
            loop_jumps=words,
        )
    elif family == "quant":
        result["quant_entries"] = (
            a["batches"] * a["rp"] * a["cp"] // (4 * info["kblock"])
        )
        result["mover_submissions"] = result["quant_entries"]
        if not info["direct"]:
            result["mover_submissions"] += 1 + a["batches"] * (a["rp"] + a["rows"])
    elif family == "mm":
        m = a["mm"]
        tiles = m.bt * (m.mp // m.tm) * (m.n // m.tn)
        chunks = m.kp // (m.nk * info["kblock"])
        result.update(
            mg_gemms=tiles * chunks,
            mg_fills=2 * tiles * chunks,
            mg_idle_posts=tiles * chunks,
            mg_drains=tiles,
            vc_trips=tiles * m.tm // 4,
            mover_submissions=tiles
            * m.tm
            * (1 + sum(o.kind != "scalar" for o in a["extras"])),
        )
    elif family == "transpose":
        words = a["nb"] * a["A"] * a["B"] // 16
        lines = words // 8
        padded = lines + info["padded_rows"]
        result.update(
            gt4_submissions=words // 2,
            vc_trips=ceildiv(lines, 8),
            mover_submissions=1 + padded + words // 2 + words,
        )
    elif family == "flat":
        result["vc_trips"] = ceildiv(ceildiv(a["n"], 128), info["trip_rows"])
    elif family == "rows":
        trips = ceildiv(a["R"], info["trip_rows"])
        blocks = a["W"] // info["width"] if info.get("wide") else 1
        if info.get("fold"):
            result["vc_dispatches"] = info["cores"] * ceildiv(trips, info["cores"])
            result["vc_column_trips"] = (
                min(trips, info["cores"]) * ceildiv(trips, info["cores"]) * blocks
            )
        else:
            result["vc_trips"] = trips * blocks
            if info.get("wide"):
                br = trips * info["trip_rows"]
                padded = (
                    ceildiv(trips * blocks, info["cores"])
                    * info["cores"]
                    * info["trip_rows"]
                )
                moves = blocks * a["R"]
                for operand in a["operands"]:
                    if operand.kind in ("full", "tile", "col"):
                        moves += (
                            1
                            + padded
                            + blocks * (br if operand.kind == "col" else a["R"])
                        )
                    elif operand.kind == "row":
                        moves += 1 + padded // 16 + blocks
                result["mover_submissions"] = moves
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=pathlib.Path)
    parser.add_argument("ledger", type=pathlib.Path)
    args = parser.parse_args()
    capture = json.loads(args.capture.read_text(encoding="utf-8"))
    summary, launches = {}, []
    for source in sorted(args.ledger.glob("overhead-*.json")):
        family = source.stem.removeprefix("overhead-")
        totals = Counter()
        for cost in json.loads(source.read_text(encoding="utf-8"))["launch_costs"]:
            index = cost["launch"]
            record = capture["records"][capture["launches"][index]["record"]]
            counts = work(family, decode(record["arguments"]), cost["class_info"])
            totals.update(counts)
            launches.append({"launch": index, "family": family, **counts})
        summary[family] = dict(totals)
    result = {
        "meaning": "Analytical class control/submission counts; no value execution, no cycles. Absent metrics are not zero. Baseline byte/step and padding differences are in overhead ledgers.",
        "totals": summary,
        "launches": launches,
    }
    (args.ledger / "work.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
