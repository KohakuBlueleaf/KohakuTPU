"""Row compaction cuts remain on DRAM-line boundaries, independent of a model."""

from kohakutpu.language import machine as MF
from kohakutpu.stack import rs

SOURCE = """target ktpu.v9

fn compact.l2(src: f16[18943, 16] @dram, dst: f16[18816, 16] @dram)
    copy dst <- src walk=((128, 4736), (147, 32)) dwalk=((128, 4704), (147, 32))
"""


def test_row_compaction_cuts_own_lines_and_kill_unaligned_mutant():
    machine = (MF.MACHINES / "multimesh_v9t6.json").read_text(encoding="utf-8")
    fixed = rs._rs.compile(SOURCE, machine, "compact", "")
    assert not fixed["verdict"]["refused"], fixed["verdict"]
    assert "16170" in fixed["l1"]
    assert "517440" in fixed["l1"]
    broken = rs._rs.compile(
        SOURCE, machine, "compact", "", mutant="l2.mover/unaligned-row-cut"
    )
    assert broken["verdict"]["refused"]
    assert "L1.VER.017" in str(broken["verdict"])
