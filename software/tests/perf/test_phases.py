"""The timed model prices a vector core beside streaming clusters only while they
stream: a kernel that quantises its A on the cores before its product (ViT FC2,
[197, 768] x [192, 768] on 2+2) costs what its two halves cost as two kernels,
not its quant at the clusters' shared fill."""

import json
import pathlib
import subprocess

import pytest

from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import kernel

HERE = pathlib.Path(__file__).parent
SOFTWARE = HERE.parents[1]
ROOT = SOFTWARE.parent
BOARD = "multimesh_v9t6"
#: Of the halves' sum: a second launch's front and node floor the fused kernel saves.
SLACK = 0.02


@pytest.fixture(scope="module")
def timed():
    cmd = ["cargo", "build", "-q", "--profile", "stage", "-p", "ktpu-perf"]
    subprocess.run(cmd + ["--bin", "timed"], cwd=SOFTWARE, check=True)
    return SOFTWARE / "target" / "stage" / "timed"


def cycles(timed, tmp_path) -> dict:
    machine = machine_of(BOARD)
    for name in ("quant", "product", "fused"):
        text = (HERE / "fixtures" / "phases" / f"{name}.ktpu").read_text("utf-8")
        l1 = kernel.compile(text, name, machine).l1_text
        (tmp_path / f"{name}.l1").write_text(l1, "utf-8")
    out = tmp_path / "timed.jsonl"
    cmd = [str(timed), "--machine", str(ROOT / "machines" / f"{BOARD}.json")]
    cmd += ["--params", str(HERE / "params.json"), "--out", str(out), "--card"]
    subprocess.run(cmd + [str(tmp_path)], check=True, capture_output=True)
    rows = [json.loads(x) for x in out.read_text("utf-8").splitlines()]
    return {r["name"]: r["cycles"] for r in rows}


def test_a_quant_before_the_product_is_priced_alone(timed, tmp_path):
    got = cycles(timed, tmp_path)
    halves = got["quant"] + got["product"]
    assert got["fused"] <= halves * (1 + SLACK), got
