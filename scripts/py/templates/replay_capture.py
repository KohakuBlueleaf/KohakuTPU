"""Replay captured source lookups offline, refusing any compiler call or library miss."""

import argparse
import json
import time
from pathlib import Path
from unittest.mock import patch

from kohakutpu.application.runtime.device import Device
from kohakutpu.stack import rs


def replay(library, capture, classes):
    device = Device(
        kind="offline",
        board=capture["board"],
        library=library,
        library_only=True,
        classes=classes,
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("captured replay invoked a compiler")

    started = time.perf_counter()
    try:
        with (
            patch.object(rs._rs, "compile", forbidden),
            patch.object(rs._rs, "compile_many", forbidden),
        ):
            for member in capture["launches"]:
                device.kernel(capture["sources"][member]["source"])
        assert device.counters["compile_calls"] == 0
        assert device.counters["library_misses"] == 0
        assert device.counters["library_hits"] == len(capture["launches"])
        return dict(
            device.counters,
            classes=classes,
            lookups=len(capture["launches"]),
            seconds=time.perf_counter() - started,
            scope="captured source selection, not card execution",
        )
    finally:
        device.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--classes", type=int, choices=[0, 1], required=True)
    args = parser.parse_args()
    capture = json.loads(args.capture.read_text(encoding="utf-8"))
    result = replay(args.library, capture, bool(args.classes))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
