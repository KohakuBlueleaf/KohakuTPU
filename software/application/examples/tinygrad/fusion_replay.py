"""Execute a host-compiled package recording on the provided model, without compiling."""

import argparse
import json
import os
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import numpy as np

from kohakutpu.application.runtime.device import Device
from kohakutpu.stack import rs


def values_from_read(raw, shape, dtype):
    """Card reads include alignment padding; tensor values do not."""
    return np.frombuffer(raw, dtype=dtype, count=int(np.prod(shape))).reshape(shape)


def reserve(memory, limits):
    """Keep recorded tensor/scratch spans separate from replay package uploads."""
    for arena, limit in zip(memory.arenas(), limits, strict=True):
        while arena.free_list[0][0] < limit:
            at, size = arena.free_list[0]
            arena.alloc(min(size, limit - at), owner="recording")


def save(path, log, shapes, board, limits):
    events = []
    for event in log:
        if event[0] == "write":
            events.append(["write", event[1], event[2].hex()])
        elif event[0] == "read":
            events.append(list(event))
        else:
            events.append(
                [
                    "run",
                    {
                        str(n): [raw.hex(), list(bindings)]
                        for n, (raw, bindings) in event[1].items()
                    },
                ]
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(board=board, shapes=shapes, events=events, limits=limits)),
        encoding="utf-8",
    )


def replay_events(card, events, shapes):
    log, cycles, values = [], [], []
    shapes = iter(shapes)
    for event in events:
        if event[0] == "write":
            log.append(("write", event[1], bytes.fromhex(event[2])))
        elif event[0] == "read":
            cycles.extend(card.replay(log))
            log.clear()
            shape, dtype = next(shapes)
            values.append(
                values_from_read(card.get(event[1], event[2]), shape, dtype).tolist()
            )
        else:
            log.append(
                (
                    "run",
                    {
                        int(n): (bytes.fromhex(raw), tuple(bindings))
                        for n, (raw, bindings) in event[1].items()
                    },
                )
            )
    cycles.extend(card.replay(log))
    return cycles, values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data = json.loads(args.record.read_text(encoding="utf-8"))
    with ExitStack() as stack:
        for owner, name in (
            (rs, "compile"),
            (rs._rs, "compile"),
            (rs._rs, "library_prebuild"),
        ):
            stack.enter_context(
                patch.object(
                    owner,
                    name,
                    side_effect=AssertionError("compiler in package replay"),
                )
            )
        dev = Device("sim", board=data["board"], nodes=1)
        try:
            reserve(dev.memory, data["limits"])
            started = time.perf_counter()
            cycles, values = replay_events(dev.card, data["events"], data["shapes"])
            result = dict(
                record=str(args.record),
                seconds=time.perf_counter() - started,
                cycles=cycles,
                values=values,
                runs=len(cycles),
                compiler_calls=0,
                semantics="frozen package/address replay, not a library hit or dynamic-shape test",
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
            print(
                json.dumps({k: v for k, v in result.items() if k != "values"}),
                flush=True,
            )
        finally:
            dev.close()


if __name__ == "__main__":
    os.environ["KTPU_RS"] = "1"
    main()
