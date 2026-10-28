"""Bounded residency on/off launch measurements on the prebuilt v9t6s model."""

import argparse
import json
import os
from pathlib import Path
from types import SimpleNamespace

import ktpu_pytest_lock
from examples.tinygrad import queue_measure as Q

from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import packages as B
from kohakutpu.compiler import _rs

ROOT = Path(__file__).resolve().parents[3]


def install_words(raw, machine):
    tables = _rs.package_read(raw, machine)
    shift = json.loads(machine)["machine"]["die"]["noc"]["data_bits"] - 4
    vc = {i for i, u in enumerate(tables["units"]) if u["type"] == 0x5643}
    total = 0
    for op, _, unit, count, arg in tables["steps"]:
        if unit not in vc or op not in (1, 9):
            continue
        first = arg if op == 1 else arg & 0xFFFFFFFF
        repeats = 1 if op == 1 else (arg >> 32) & 0xFFFF
        total += repeats * sum(
            w >> shift == 1 for w in tables["payloads"][first : first + count]
        )
    return total


def measure(workload, resident, limit=4, seconds=90):
    os.environ["KOHAKUTPU_RESIDENT"] = str(int(resident))
    os.environ["KOHAKUTPU_QUEUE"] = "0"
    dev = D.Device(
        kind="sim",
        board="vlt_v9t6s_1n",
        build=ROOT / "build/v9t6s/vlt_card_v9t6s_1n",
        nodes=1,
    )
    dev.card.alarm = seconds
    built = B.built
    counts = []

    def counted(progs, machine, fetch=None, residents=None, after=None):
        if len(counts) >= limit:
            raise StopIteration(f"measurement prefix limited to {limit} kernels")
        batch = []
        for n, p in progs.items():
            cores = residents[n].snapshot() if resident and residents else None
            batch.append(
                (n, install_words(p.bound(False, after, cores=cores), p.machine_json))
            )
        result = built(progs, machine, fetch, residents, after)
        counts.extend(batch)
        return result

    B.built = counted
    try:
        result = Q.measure(dev, workload, seconds=seconds, batch=1)
        result["resident"] = resident
        result["prefix_limit"] = limit
        if result.get("error", "").startswith("StopIteration: measurement prefix"):
            result["stop_reason"] = result.pop("error")
        for event, (node, words) in zip(result["events"], counts, strict=True):
            assert event["node"] == node
            event["imem_words"] = words
        return result
    finally:
        B.built = built
        dev.card.t.close()
        dev.closed = True
        for arena in dev.memory.arenas():
            arena.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workload", choices=("activation", "lenet_forward"))
    parser.add_argument("--seconds", type=float, default=90)
    parser.add_argument("--limit", type=int, default=4)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="measurement JSON, appended to when it exists",
    )
    args = parser.parse_args()
    if not 0 < args.seconds < 600 or args.limit < 1:
        parser.error("seconds must be in (0, 600); limit must be positive")
    ktpu_pytest_lock.pytest_configure(SimpleNamespace())
    output = args.output
    data = (
        json.loads(output.read_text(encoding="utf-8"))
        if output.exists()
        else {"model": "v9t6s/vlt_card_v9t6s_1n", "measurements": []}
    )
    workloads = (args.workload,) if args.workload else ("activation", "lenet_forward")
    try:
        for workload in workloads:
            pair = [
                measure(workload, resident, args.limit, args.seconds)
                for resident in (False, True)
            ]
            if all("output" in r for r in pair):
                data[f"{workload}_outputs_equal"] = (
                    pair[0]["output"] == pair[1]["output"]
                )
            for result in pair:
                result.pop("output", None)
                data["measurements"].append(result)
                print(json.dumps(result), flush=True)
    finally:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        ktpu_pytest_lock.pytest_unconfigure(SimpleNamespace())


if __name__ == "__main__":
    main()
