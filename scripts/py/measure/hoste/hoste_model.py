"""Execute host-generated eager wire witnesses on one read-only Verilated build."""

import argparse
import gzip
import json
import pickle
import time
from pathlib import Path

import numpy as np
from kohakutpu.application.runtime.card import VerilatorCard


def load(path):
    with gzip.open(path, "rb") as source:
        return pickle.load(source)  # Trusted transcripts produced by hoste_witness.py.


def execute(card, bundle, limit, lists=False, budget=None):
    started = time.monotonic()
    reads = {}
    launches = []
    cpu = time.process_time()
    skip_until = 0
    selections = []
    preparation_cpu = replay_cpu = 0.0
    before_uploads = sum(q.counters["upload_bytes"] for q in card.queues.values())
    for index, event in enumerate(bundle["events"]):
        if index < skip_until:
            continue
        if time.monotonic() - started > limit:
            raise TimeoutError(f"transcript exceeded {limit}s at event {index}")
        if event[0] == "write":
            card.write(event[1], event[2])
        elif event[0] == "read":
            reads[index] = card.get(event[1], event[2])
        elif lists or budget is not None:
            stop = index + 1
            while stop < len(bundle["events"]) and bundle["events"][stop][0] == "run":
                stop += 1
            t0 = time.process_time()
            prepared = card.prepare_recorded(
                bundle["events"][index:stop], (128 << 20) if budget is None else budget
            )
            selections.append(prepared.selection)
            try:
                grouped, groups = (
                    card.prepare_execution(prepared)
                    if lists
                    else (prepared, [(i,) for i in range(len(prepared))])
                )
                preparation_cpu += time.process_time() - t0
                t0 = time.process_time()
                for (kind, programs), group in zip(grouped, groups, strict=True):
                    submit = (
                        card.submit_execution
                        if kind == "list"
                        else card.submit_prepared
                    )
                    result = card.collect(submit(programs))
                    launches.append(
                        {
                            "event": index,
                            "count": len(group),
                            "result": {
                                key: result[key]
                                for key in ("cycles", "record")
                                if key in result
                            },
                        }
                    )
                replay_cpu += time.process_time() - t0
            finally:
                prepared.close()
            skip_until = stop
        else:
            t0 = time.monotonic()
            tags = {
                node: card._submit(node, raw, bindings)
                for node, (raw, bindings) in event[1].items()
            }
            completions = []
            for node, tag in tags.items():
                completion = card.queues[node].wait(tag, timeout=card.timeout)
                card._segment(node, tag)
                completions.append((node, completion.cycles, completion.record))
            launches.append(
                {"event": index, "wall": time.monotonic() - t0, "nodes": completions}
            )
        if index % 20 == 0:
            print(
                json.dumps(
                    {
                        "event": index,
                        "events": len(bundle["events"]),
                        "wall": time.monotonic() - started,
                    }
                ),
                flush=True,
            )
    outputs = {}
    covered = set()
    for name, point in bundle["checkpoints"].items():
        indices = [i for i in range(point["start"], point["end"]) if i in reads]
        assert indices, f"{name}: no device read in output checkpoint"
        covered.update(indices)
        outputs[name] = b"".join(reads[i] for i in indices)
    return outputs, {
        "wall": time.monotonic() - started,
        "cpu": time.process_time() - cpu,
        "launches": launches,
        "preparation_cpu": preparation_cpu,
        "replay_cpu": replay_cpu,
        "selections": selections,
        "upload_bytes": sum(q.counters["upload_bytes"] for q in card.queues.values())
        - before_uploads,
        "unlabelled_reads": sorted(set(reads) - covered),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("off", type=Path)
    ap.add_argument("on", type=Path)
    ap.add_argument("--build", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--limit", type=float, default=240)
    ap.add_argument("-n", type=int, choices=(1,), default=1)
    ap.add_argument("--steps", type=int)
    ap.add_argument(
        "--single",
        action="store_true",
        help="Execute only off; save arrays for a separately bounded comparison",
    )
    ap.add_argument("--lists", action="store_true")
    ap.add_argument("--baseline-budget", type=int)
    ap.add_argument("--replay-budget", type=int)
    args = ap.parse_args()
    left, right = load(args.off), load(args.on)
    if args.steps is not None:
        for bundle in (left, right):
            bundle["checkpoints"] = {
                k: v
                for k, v in bundle["checkpoints"].items()
                if int(k.split("-")[0][4:]) < args.steps
            }
            bundle["events"] = bundle["events"][
                : max(v["end"] for v in bundle["checkpoints"].values())
            ]
            bundle["configuration"]["executed_steps"] = args.steps
    assert left["configuration"]["board"] == right["configuration"]["board"]
    assert left["checkpoints"].keys() == right["checkpoints"].keys()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    card = VerilatorCard(
        args.build,
        board=left["configuration"]["board"],
        nodes=(0,),
        log=args.output.with_suffix(".model.log"),
    )
    try:
        start = left["summary"]["arena_start"]
        end = max(b["summary"]["high_water"] for b in (left, right))
        assert card.memory.dram.alloc(end - start, owner="transcript") == start
        for node, (lo, hi) in left["summary"]["staging"].items():
            end = max(hi, right["summary"]["staging"][node][1])
            if end > lo:
                assert (
                    card.memory.staging[node].alloc(end - lo, owner="transcript") == lo
                )
        first, timing0 = execute(card, left, args.limit, budget=args.baseline_budget)
        if args.single:
            arrays = {
                name: np.frombuffer(value, dtype=np.uint8)
                for name, value in first.items()
            }
            np.savez(args.output.with_suffix(".npz"), **arrays)
            report = {
                "single": True,
                "configuration": left["configuration"],
                "timing": timing0,
                "arrays": len(first),
                "output_bytes": sum(map(len, first.values())),
            }
            args.output.write_text(
                json.dumps(report, indent=2) + "\n", encoding="utf-8"
            )
            print(
                json.dumps({k: v for k, v in report.items() if k != "timing"}),
                flush=True,
            )
            return
        second, timing1 = execute(
            card, right, args.limit, args.lists, args.replay_budget
        )
        differences = {
            name: sum(a != b for a, b in zip(first[name], second[name], strict=True))
            for name in first
        }
        nonzero = sum(sum(byte != 0 for byte in value) for value in first.values())
        assert nonzero, "all output bytes were zero"
        arrays = {
            name: np.frombuffer(value, dtype=np.uint8) for name, value in first.items()
        }
        np.savez(args.output.with_suffix(".npz"), **arrays)
        report = {
            "bit_identical": not any(differences.values()),
            "differing_bytes": differences,
            "lists": args.lists,
            "baseline_budget": args.baseline_budget,
            "replay_budget": args.replay_budget,
            "arrays": len(first),
            "output_bytes": sum(map(len, first.values())),
            "nonzero_bytes": nonzero,
            "board": left["configuration"]["board"],
            "configuration": left["configuration"],
            "native_host_execution": "offline transcript generation, not local model host CPU",
            "wire_identical": left["events"] == right["events"],
            "off": timing0,
            "on": timing1,
        }
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(
            json.dumps({k: v for k, v in report.items() if k not in ("off", "on")}),
            flush=True,
        )
        assert report["bit_identical"], f"output bytes differ: {differences}"
    finally:
        card.close()


if __name__ == "__main__":
    main()
