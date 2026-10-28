"""DRAM capacity and the model compiler's depth share one build source."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from kohakutpu.driver.clock.card import load_board
from kohakutpu.language import machine

ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = ROOT / "scripts" / "py"


def script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("words", [1 << 18, 1 << 20])
def test_model_capacity_row_follows_the_build_depth(words):
    code = (
        "from types import SimpleNamespace; from machine.table import dram_bytes; "
        f"b = {{'model': {{'dram_words': {words}}}}}; "
        "i = SimpleNamespace(board_dict=b, board=lambda k: (b[k], 'board:'+k), "
        "cfg=SimpleNamespace(meshes=lambda: range(4))); "
        "print(dram_bytes(i)[0])"
    )
    got = subprocess.run(
        [sys.executable, "-c", code],
        cwd=SCRIPTS,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert int(got.stdout) == words * 64 * 4


def test_model_file_is_the_builds_capacity():
    board = load_board("vlt_v9t6s_1n")
    assert "mem_words" not in board
    assert (
        machine.load(board["name"]).card.dram.bytes
        == board["model"]["dram_words"] * 64 * 4
    )
    assert "model.dram_words" in machine.load(board["name"]).source("card.dram.bytes")


def test_model_build_passes_the_board_depth_without_running_verilator(monkeypatch):
    mod = script("card_model")
    calls = []
    monkeypatch.setattr(mod, "vlt", lambda *args: calls.append(args))
    monkeypatch.setattr(
        sys,
        "argv",
        ["card_model", "card_v9t6s_1n", "--board", "vlt_v9t6s_1n", "--no-pgo"],
    )
    assert mod.main() == 0
    board = json.loads((ROOT / "boards/vlt_v9t6s_1n.json").read_text("utf-8"))
    assert calls[0][2] == ["-G", f"DRAM_WORDS={board['model']['dram_words']}"]
    sys.argv += ["-G", "DRAM_WORDS=1"]
    with pytest.raises(SystemExit, match="comes from the model board"):
        mod.main()
    assert len(calls) == 1
