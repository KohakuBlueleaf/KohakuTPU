"""The timed model against its recorded anchors: within its budget as it stands, and
out of it with any parameter broken, so no parameter sits unpinned. A board's Verilator
truth reads a parameter's `value`, its card truth the `card` column."""

import json
import pathlib
import subprocess

import compare
import pytest

HERE = pathlib.Path(__file__).parent
SOFTWARE = HERE.parents[1]
ROOT = SOFTWARE.parent
PARAMS = json.loads((HERE / "params.json").read_text("utf-8"))
VERILATOR = sorted((HERE / "fixtures").glob("anchors.*.jsonl"))
CARD = sorted((HERE / "fixtures").glob("card.*.jsonl"))
#: Parameters no anchor reaches yet: only a run over two nodes or a launch moves them.
UNPINNED = ("ring", "launch_front", "launch_gap")
BUDGET = 0.20
#: The card rows the card column holds: past the Xache, or free of memory. The column
#: is a step's cold memory, so a warm walk under 8 MB reads over it by design.
COLD = (
    "mem_copy_16m",
    "mem_copy_64m",
    "mem_stride_16m",
    "mem_read_16m",
    "mem_write_16m",
    "pack_mx7",
    "exp_256",
    "mul_256",
    "fe_sync",
    "long_256",
    "loop_1024",
)


@pytest.fixture(scope="session")
def timed():
    cmd = ["cargo", "build", "-q", "--profile", "stage", "-p", "ktpu-perf"]
    subprocess.run(cmd + ["--bin", "timed"], cwd=SOFTWARE, check=True)
    return SOFTWARE / "target" / "stage" / "timed"


def worst(timed, truth, tmp_path, params, card=False, machine=None) -> float:
    board = truth.stem.split(".", 1)[1]
    path = tmp_path / "params.json"
    path.write_text(json.dumps(params), "utf-8")
    out = tmp_path / "model.jsonl"
    machine = machine or ROOT / "machines" / f"{board}.json"
    cmd = [str(timed), "--machine", str(machine)]
    cmd += ["--params", str(path), "--out", str(out), str(HERE / "anchors")]
    cmd += [str(HERE / "anchors" / "memory"), "--card"] if card else []
    subprocess.run(cmd, check=True, capture_output=True)
    got = compare.anchors(truth, out, verbose=False)
    assert len(got) >= 14
    rows = [v for k, v in got.items() if not card or k in COLD]
    return max(abs(v["error"]) for v in rows)


@pytest.mark.parametrize("truth", VERILATOR, ids=lambda p: p.stem)
def test_the_model_holds_its_anchors(timed, truth, tmp_path):
    assert worst(timed, truth, tmp_path, PARAMS) <= BUDGET


@pytest.mark.parametrize("truth", CARD, ids=lambda p: p.stem)
def test_the_card_column_holds_its_anchors(timed, truth, tmp_path):
    assert worst(timed, truth, tmp_path, PARAMS, card=True) <= BUDGET


@pytest.mark.parametrize("truth", VERILATOR, ids=lambda p: p.stem)
@pytest.mark.parametrize("name", [k for k in PARAMS if k not in UNPINNED])
def test_a_broken_parameter_leaves_the_budget(timed, truth, tmp_path, name):
    """A parameter the machine holds (its `fact`) is broken in the machine file."""
    broken = json.loads(json.dumps(PARAMS))
    if "fact" not in broken[name]:
        broken[name]["value"] *= 3
        assert worst(timed, truth, tmp_path, broken) > BUDGET
        return
    board = truth.stem.split(".", 1)[1]
    doc = json.loads((ROOT / "machines" / f"{board}.json").read_text("utf-8"))
    *head, last = broken[name]["fact"].split(".")
    section = doc["machine"]
    for key in head:
        section = section[key]
    section[last] *= 3
    machine = tmp_path / f"{board}.json"
    machine.write_text(json.dumps(doc), "utf-8")
    assert worst(timed, truth, tmp_path, broken, machine=machine) > BUDGET
