"""Check the reported reduced-LeNet exact key against a fresh prebuild."""

import json
from pathlib import Path

from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import rs

ROOT = Path(__file__).resolve().parents[3]


def main():
    capture = json.loads(
        (ROOT / ".plan/vq/loop7/check-prebuild/capture.json").read_text()
    )
    key = "c4cc4159d7a8970c14402929a6e46648f769bd21e75451d7288d8b3eacd0f0a3"
    source = capture["sources"][key]["source"]
    machine = rs.machine_json(machine_of("vlt_v9t6s_1n"))
    identity = json.loads(rs._rs.library_key(source, machine))
    assert identity["class"] == key
    result = rs._rs.library_get(str(ROOT / "build/vq/loop7-check"), source, machine)
    assert result is not None
    print(
        json.dumps(
            {"identity": identity, "hit": True, "name": json.loads(source)["name"]}
        )
    )


if __name__ == "__main__":
    main()
