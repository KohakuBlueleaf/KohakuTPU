"""Whole models on the card against their baselines: final verification, run as
root on the host holding the card, at the work profile, from a prebuilt library."""

import json
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
EXAMPLES = ROOT / "software" / "kohakutpu" / "application" / "examples" / "tinygrad"
BASELINES = json.loads(
    (ROOT / "software" / "tests" / "fixtures" / "baselines.json").read_text("utf-8")
)
BOARD = "multimesh_v9t6"
CARD = [
    "--device",
    "card",
    "--transport",
    "xdma",
    "--board",
    BOARD,
    "--nodes",
    "1",
    "--library",
    str(ROOT / "build" / "templates"),
    "--library-only",
]

pytestmark = pytest.mark.skipif(
    "KOHAKUTPU_PROFILE" not in os.environ, reason="the card's work profile is not set"
)


def run(script, *args):
    env = {**os.environ, "PYTHONPATH": str(EXAMPLES.parent.parent)}
    cmd = [sys.executable, str(EXAMPLES / script), *args, *CARD]
    out = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr[-2000:]
    return out.stdout


def test_lenet_trains(tmp_path):
    want = BASELINES["lenet"]
    report = tmp_path / "lenet.json"
    args = ["--model", "lenet", "--batch", "8", "--steps", str(want["steps"])]
    run("mnist_mlp.py", *args, "--log", "30", "--test", "256", "--report", str(report))
    got = json.loads(report.read_text("utf-8"))
    assert got["curves"]["test"][-1]["accuracy"] >= want["accuracy"]
    assert got["counters"]["compile_calls"] == 0


def test_vit_matches_its_cpu_reference():
    want = BASELINES["vit_tiny"]
    args = ["--blocks", "12", "--batch", "1", "--inputs", str(want["inputs"])]
    got = json.loads(run("vit_tiny.py", *args).strip().splitlines()[-1])
    assert got["top1_agreement"] >= want["top1"]
    assert got["counters"]["cycles"] <= want["cycles"]
