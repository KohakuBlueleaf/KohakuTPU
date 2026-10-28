"""Prove final image emission matches the packages exercised in Loop7 sessions."""

import json
import pickle
from pathlib import Path

from kohakutpu.stack import rs

ROOT = Path(__file__).resolve().parents[3]


def main():
    count = 0
    for name in ("q0", "q1", "banks", "validation", "negative-fixed", "direct-witness"):
        bundle = pickle.loads((ROOT / f".plan/vq/loop7/{name}.pkl").read_bytes())
        for case in bundle["cases"]:
            new = rs._rs.compile(
                json.dumps(case["graph"]), bundle["machine"], cache_dir="", fetch=True
            )
            assert not new["verdict"]["refused"], new["verdict"]
            assert new["l1"] == case["compiled"]["l1"], case["label"]
            assert new["packages"] == case["compiled"]["packages"], case["label"]
            count += 1
    print("final-emitter equality:", count, "measured/reference packages")


if __name__ == "__main__":
    main()
