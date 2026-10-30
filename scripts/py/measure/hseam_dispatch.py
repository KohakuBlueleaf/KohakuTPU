"""Measure the installed firmware's refusal of a card-side list descriptor.

This probes reserved opcode 3, not a new firmware implementation. Only the
read-only model is opened; the descriptor bytes live in scratch-owned DRAM.
"""

import argparse
import json
import struct
import time
from pathlib import Path
from types import SimpleNamespace

import ktpu_pytest_lock
from kohakuaccel.driver.node.queue import NodeError
from kohakutpu.application.runtime import device as D


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = {"implementation": "installed firmware reserved-opcode probe", "runs": []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    ktpu_pytest_lock.pytest_configure(SimpleNamespace())
    try:
        dev = D.Device("sim", board="vlt_v9t6s_1n", nodes=1, build=args.build)
        try:
            physical = dev.card.alloc(64)
            dev.card.write(
                physical, struct.pack("<8Q", 0x315453494C414B, 1, 1, 0, 0, 0, 0, 0)
            )
            for op in (0, 3, 4, 5):
                start = time.perf_counter()
                calls = dev.card.t.calls
                tag = dev.card.q.submit(None, op=op, words=[physical, 64])
                try:
                    completion = dev.card.q.wait(tag, timeout=dev.card.timeout)
                except NodeError as error:
                    completion = error.completion
                row = {
                    "opcode": op,
                    "tag": tag,
                    "status": completion.status,
                    "name": completion.name,
                    "detail": completion.detail,
                    "wall_seconds": time.perf_counter() - start,
                    "transport_calls": dev.card.t.calls - calls,
                    "record": completion.record,
                }
                result["runs"].append(row)
                assert completion.status == (0 if op == 0 else 0x40)
            dev.card.free(physical)
        finally:
            dev.close()
    finally:
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        ktpu_pytest_lock.pytest_unconfigure(SimpleNamespace())
    print(json.dumps(result))


if __name__ == "__main__":
    main()
