"""KohakuAccel OS on the Verilated card models (model host).

    cd software/os/tests/host
    KOS_CARD_MODELS=~/ktpu/build KOS_OUT=~/kos/out \\
    KOS_DRIVER_PATH=~/ktpu/software/driver:~/ktpu/software/language:~/ktpu/software/compiler \\
    pytest --rootdir . test_card.py -n 1

Each case runs tools/run_card.py on a fresh card model (no paint: its DRAM
starts zero) with one interaction: every node exits 0, no FAIL line, and the
case's own facts from the "@ name value" lines. A card model is the slow
model, so each case is one command, and only what the core models cannot
show runs here: units, MG dispatch, the mover and the dispatch engine (remote
spawn and messages across meshes are test_core's core2 cases).
"""

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
OUT = pathlib.Path(os.environ.get("KOS_OUT", pathlib.Path.home() / "kos/out"))
DRIVER = os.environ.get("KOS_DRIVER_PATH", "")

C1N = MODELS / "v9t6/vlt_card_v9t6_1n"
C2N = MODELS / "t2s/vlt_card_v9t6_2n"

pytestmark = pytest.mark.skipif(
    not DRIVER or not C1N.exists(), reason="no card models or driver path"
)
# The 2-die model runs ~8 k sys cycles/s and one app launch (boot, init, the
# app's spawn) is ~0.5 M core cycles: a 2-die case takes ~2 minutes, over the
# per-case minute, so it runs only when asked for (KOS_SLOW=1).
needs_2n = pytest.mark.skipif(
    not C2N.exists() or os.environ.get("KOS_SLOW") != "1",
    reason="no 2-die model, or KOS_SLOW unset (2-die cases take ~2 min)",
)


def card(
    tmp: pathlib.Path, board: str, model: pathlib.Path, cmd: str, flags: str = ""
) -> dict:
    out = tmp / "run.json"
    args = [
        sys.executable,
        str(TOOLS / "run_card.py"),
        "--board",
        board,
        "--model",
        str(model),
    ]
    args += ["--image", str(OUT / "kos.elf"), "--cmd", cmd, "--flags", flags]
    args += ["--out", str(tmp / "run"), "--json", str(out), "--timeout", "600"]
    env = dict(os.environ, PYTHONPATH=DRIVER)
    p = subprocess.run(
        args, cwd=TOOLS, env=env, capture_output=True, text=True, check=False
    )
    assert "FAIL" not in p.stdout.replace("run_card: PASS", ""), p.stdout[-4000:]
    assert p.returncode == 0, p.stdout[-4000:] + p.stderr[-2000:]
    return json.loads(out.read_text())


def test_units_1n(tmp_path):
    """Every unit of the board answers CU_CTRL with its board type."""
    r = card(tmp_path, "card_v9t6_1n", C1N, "units")
    assert r["measures"]["0"]["units.count"] == 4


@pytest.mark.parametrize("flags", ["", "engine"])
def test_mg_1n(tmp_path, flags):
    """FILL/FILL/DRAIN through the mailbox (and with `engine` a round through
    the dispatch engine); the completion wakes the waiting thread."""
    r = card(tmp_path, "card_v9t6_1n", C1N, "mg", flags)
    m = r["measures"]["0"]
    assert m["mg.units"] == 2
    assert ("mg.engine_round_cycles" in m) == (flags == "engine")


def test_mover_1n(tmp_path):
    card(tmp_path, "card_v9t6_1n", C1N, "mover 32")


@needs_2n
def test_remote_units_2n(tmp_path):
    """Die 1's units through die 1's kernel."""
    r = card(tmp_path, "card_v9t6_2n", C2N, "units")
    assert r["measures"]["0"]["units.count"] == 8


@needs_2n
def test_remote_mg_2n(tmp_path):
    """Die 1's MG units dispatched and waited for by die 1's kernel, DRAIN
    written across the interlink into die 0's landing zone. Spans are carved:
    with both at the same offset the model's aliased DRAM let node 1
    overwrite node 0's span."""
    r = card(tmp_path, "card_v9t6_2n", C2N, "mg")
    assert r["measures"]["0"]["mg.units"] == 4


@needs_2n
def test_mover_land_2n(tmp_path):
    """A mover copy into die 1's landing zone, checked by a thread there."""
    card(tmp_path, "card_v9t6_2n", C2N, "mover 32")
