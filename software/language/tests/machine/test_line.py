"""The write-line row follows the image-selected Xache wrapper, not a word."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from kohakutpu.language import machine

ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = ROOT / "scripts" / "py"
ROW = """
import json, sys
from pathlib import Path
from machine import table
from machine.read import image
image.ROOT = Path(sys.argv[1])
img = object.__new__(image.Image)
img.ver = 'v9t6'
print(json.dumps(table.dram_line_bytes(img)))
"""


def row(root):
    got = subprocess.run(
        [sys.executable, "-c", ROW, str(root)],
        cwd=SCRIPTS,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return json.loads(got.stdout)


@pytest.mark.parametrize("bits", [256, 512, 1024])
def test_line_row_follows_the_build_selected_wrapper(tmp_path, bits):
    files = {
        "scripts/tcl/multimesh_v9t6_bd.tcl": "set V8_STAGES $here/fixture\n",
        "scripts/tcl/fixture/35_xache.tcl": (
            "set kx [create_bd_cell -type module -reference fixture_xache xache]\n"
        ),
        "xilinx-fpga/xcvu13p/bd/fixture_xache.v": (
            f"localparam integer M = 4, W = {bits}, AW = 40;\n"
        ),
    }
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    value, source = row(tmp_path)
    assert value == bits // 8
    assert "fixture_xache.v:1" in source
    assert "fixture/35_xache.tcl:1" in source
    assert "multimesh_v9t6_bd.tcl:1" in source


def test_line_fact_is_separate_from_the_noc_word():
    for name in machine.names():
        m = machine.load(name)
        assert (m.card.dram.line_bytes, m.die.word_bytes) == (64, 32)
        assert "kx_pbd_4x4.v:328" in m.source("card.dram.line_bytes")
