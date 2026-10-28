"""Replay the captured non-lanes launches against the current named machine."""

import argparse
import json
from pathlib import Path

import derive

from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.stack import rs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    doc = json.loads(args.capture.read_text(encoding="utf-8"))
    doc["machine_json"] = rs.machine_json(OfflineCard(doc["board"]).machine)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    capture = args.output.parent / "current-machine-capture.json"
    capture.write_text(json.dumps(doc), encoding="utf-8")
    derive.run(capture, args.output, "exact", 256, False, *(["all"] * 7))


if __name__ == "__main__":
    main()
