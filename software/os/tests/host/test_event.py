"""Lead card gates for record-copy and remote-batch witnesses (node IDs only)."""

import json
import os
import pathlib
import subprocess
import sys

import pytest

TOOLS = pathlib.Path(__file__).resolve().parents[2] / "tools"
MODELS = pathlib.Path(
    os.environ.get("KOS_CARD_MODELS", pathlib.Path.home() / "ktpu/build")
)
IMAGE = pathlib.Path(
    os.environ.get("KOS_HOT_IMAGE", "build/oshot/loop4/checked/13/kos.elf")
)


def test_event_records(tmp_path):
    result = tmp_path / "result.json"
    subprocess.run(
        [
            sys.executable,
            str(TOOLS / "run_card.py"),
            "--board",
            "card_v9t6_1n",
            "--model",
            str(MODELS / "v9t6/vlt_card_v9t6_1n"),
            "--image",
            str(IMAGE),
            "--cmd",
            "mg 0; kevent",
            "--flags",
            "engine",
            "--timeout",
            "360",
            "--out",
            str(tmp_path / "run"),
            "--json",
            str(result),
        ],
        check=True,
    )
    data = json.loads(result.read_text())
    assert data["ok"]
    assert data["measures"]["0"]["kevent.failures"] == 0
    assert "retired 3 -> 6" in data["nodes"][0]["log"]


@pytest.mark.parametrize("kind", ["batch", "event"])
def test_card_mutation(tmp_path, kind):
    model = "t2s/vlt_card_v9t6_2n" if kind == "batch" else "v9t6/vlt_card_v9t6_1n"
    subprocess.run(
        [
            sys.executable,
            str(TOOLS / "hotpaths/witness.py"),
            kind,
            "--model",
            str(MODELS / model),
            "--out",
            str(tmp_path),
        ],
        check=True,
    )
    assert json.loads((tmp_path / "result.json").read_text())["killed"]
