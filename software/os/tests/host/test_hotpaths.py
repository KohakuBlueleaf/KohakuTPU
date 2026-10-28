"""One semantic witness for each hot-path state rule; select individual node IDs."""

import os
import pathlib
import re
import subprocess
import sys

import pytest

TOOLS = pathlib.Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS))

from boot import load
from run_core import run

BUILD = pathlib.Path(os.environ.get("KOS_BUILD", pathlib.Path.home() / "kos/build"))
CASES = {
    "leaf": (1, ["KOS_FAST", "KOS_CHAN_FIFO"]),
    "l0": (8, ["KOS_L0_FAST", "KOS_L0_SPLIT"]),
    "registers": (2, ["KOS_SPLIT"]),
    "translation": (4, ["KOS_XLAT"]),
    "wake": (5, ["KOS_POLL_EVENTS"]),
    "fifo": (6, ["KOS_CHAN_FIFO"]),
}


@pytest.mark.parametrize("case", CASES)
def test_hotpath_witness(tmp_path, case):
    number, flags = CASES[case]
    results = []
    for mutant in (0, number):
        out = tmp_path / str(mutant)
        args = [sys.executable, str(TOOLS / "build.py"), "kos", "--out", str(out)]
        for flag in [*flags, f"KOS_HOT_MUT={mutant}"]:
            args += ["-D", flag]
        subprocess.run(args, check=True, capture_output=True, text=True)
        board = "core4s" if case == "l0" else "core2"
        result = run(
            load(board),
            BUILD / board,
            out / "kos.elf",
            f"khot {number}",
            max_cycles=2_000_000,
        )
        results.append(result)
    good, bad = results
    assert good["rc"] == 0 and not good["fails"], good["output"]
    assert good["measures"]["0"]["khot.failures"] == 0
    assert bad["measures"]["0"]["khot.failures"] > 0, bad["output"]


def test_small_syscall_frame(tmp_path):
    sizes = []
    for small in (False, True):
        out = tmp_path / str(small)
        args = [sys.executable, str(TOOLS / "build.py"), "kos", "--out", str(out)]
        if small:
            args += ["-D", "KOS_SMALL_CALL"]
        subprocess.run(args, check=True, capture_output=True, text=True)
        listing = (out / "kos.lst").read_text()
        frame = re.search(r"<k_syscall>:[\s\S]*?addi\s+sp, sp, -0x([0-9a-f]+)", listing)
        sizes.append(int(frame[1], 16))
    assert sizes[1] <= 128
    assert sizes[0] > 128  # removing the split violates the frame bound


def test_event_gather_size(tmp_path):
    sizes = []
    for asm in (False, True):
        out = tmp_path / str(asm)
        args = [sys.executable, str(TOOLS / "build.py"), "kos", "--out", str(out)]
        if asm:
            args += ["-D", "KOS_EVENT_ASM"]
        subprocess.run(args, check=True, capture_output=True, text=True)
        symbols = [
            line.split()
            for line in (out / "kos.map").read_text().splitlines()
            if line.split() and line.split()[-1] == "k_event_out"
        ]
        sizes.append(int(symbols[0][2], 16))
    assert 0 < sizes[1] < sizes[0]
