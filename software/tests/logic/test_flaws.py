"""Silicon flaws: each machine file says which its image carries, and a handling
runs exactly where its flaw is present (docs/spec/stack/flaws.md)."""

import json
import sys
from pathlib import Path

import pytest

from kohakutpu.application.runtime.device import submission
from kohakutpu.language import examples
from kohakutpu.language.machine import MACHINES
from kohakutpu.stack import rs

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts" / "py"))

from machine.read import image

#: Images whose mover has the W skid F-001 needs (KOHAKU_IL_MVSKID=1).
SKIDDED = {"multimesh_v9t5", "multimesh_v9t6", "multimesh_v9t7", "multimesh_v9t8"}
#: Flaws of units these images do not build (SIMT shader core, SRU).
UNITLESS = {"F-010", "F-011", "F-013"}


def machine(name: str) -> dict:
    return json.loads((MACHINES / f"{name}.json").read_text("utf-8"))


@pytest.mark.parametrize(
    "name",
    ["multimesh_v9", "multimesh_v9t4", "multimesh_v9t5", "multimesh_v9t8"],
)
def test_presence_follows_the_image(name):
    flaws = machine(name)["machine"]["flaws"]
    assert flaws["F-001"]["present"] == (name in SKIDDED)
    assert all(flaws[f]["present"] != (f in UNITLESS) for f in flaws if f != "F-001")


def test_a_flaw_is_read_both_ways_and_an_unknown_line_stops(monkeypatch):
    lines = {
        "bad.v": ("x", "y <= q;"),
        "good.v": ("y <= ok ? q : y;",),
        "new.v": ("z",),
    }
    monkeypatch.setattr(image, "_lines", lambda path: lines[path.name])
    flawed, fixed = r"y <= q;", r"y <= ok \? q : y;"
    assert image.flaw_line("bad.v", flawed, fixed=fixed) == (True, "bad.v:2")
    assert image.flaw_line("good.v", flawed, fixed=fixed) == (False, "good.v:1")
    with pytest.raises(SystemExit):
        image.flaw_line("new.v", flawed, fixed=fixed)
    with pytest.raises(SystemExit):
        image.flaw_line("new.v", flawed)


def compiled(doc: dict, name: str = "lone"):
    text = examples.example(f"mover/{name}", "l2").read_text("utf-8")
    return rs.compile(text, name, json.dumps(doc))


def lowered(doc: dict, name: str = "lone") -> str:
    return compiled(doc, name)["l1"]


def test_a_copy_overlapping_its_source_reads_it_whole_first():
    """L2.MOV.021, on a machine without F-001: every other word of x packed
    into x's front goes source to scratch, then scratch to destination."""
    doc = machine("multimesh_v9t8")
    doc["machine"]["flaws"]["F-001"]["present"] = False
    copies = [
        line for line in lowered(doc, "overlap").splitlines() if "copy w(" in line
    ]
    assert len(copies) == 2, copies
    assert "-> w(h0" in copies[0].replace(" + ", "+")
    assert "copy w(h0" in copies[1].replace(" + ", "+")


def test_a_copy_writing_one_word_runs_runs_on_the_vector_cores():
    l1 = lowered(machine("multimesh_v9t8"))
    assert "vdrain a1 <- l1[0]" in l1
    assert "desc a1=y+0 walk=(64, 256)" in l1
    assert "copy w(" not in l1


def test_a_copy_whose_page_cut_can_leave_one_beat_runs_on_the_vector_cores():
    """L2.MOV.020: runs of 25 words 26 apart, no one-word run."""
    l1 = lowered(machine("multimesh_v9t8"), "pagetail")
    assert "vdrain a1 <- l1[0]" in l1
    assert "copy w(" not in l1
    doc = machine("multimesh_v9t8")
    doc["machine"]["flaws"]["F-001"]["present"] = False
    assert "vdrain" not in lowered(doc, "pagetail")


def test_a_gather_into_odd_strided_rows_packs_then_copies_on_the_vector_cores():
    """L2.PASS.001: two-word rows three words apart."""
    l1 = lowered(machine("multimesh_v9t8"), "oddrows").replace(" + ", "+")
    gathers = [line for line in l1.splitlines() if "gather " in line]
    assert gathers and all("-> w(h0" in line for line in gathers), gathers
    assert "vdrain a1 <- l1[0]" in l1
    doc = machine("multimesh_v9t8")
    doc["machine"]["flaws"]["F-001"]["present"] = False
    plain = lowered(doc, "oddrows").replace(" + ", "+")
    assert "-> w(y+0" in plain and "h0" not in plain


def test_only_a_port_a_mover_proof_leans_on_must_bind_on_a_write_line():
    """L1.MOV.019: the scatter's rows are proven from its table's base, so
    `t` must start on a line; the rows read and the index list need nothing."""
    doc = machine("multimesh_v9t8")
    line = submission.burst_line(json.dumps(doc))
    assert line == doc["machine"]["card"]["dram"]["line_bytes"] == 64
    ports = compiled(doc, "scatter")["f001"]["line_ports"]
    assert ports == ["t"], ports
    assert submission.unaligned({"s": 96, "idx": 32, "t": 4160}, line, ports) == []
    assert submission.unaligned({"s": 96, "idx": 32, "t": 96}, line, ports) == ["t"]
    doc["machine"]["flaws"]["F-001"]["present"] = False
    assert compiled(doc, "scatter")["f001"]["line_ports"] == []
    assert (
        submission.unaligned({"t": 96}, submission.burst_line(json.dumps(doc)), ports)
        == []
    )


def test_a_copy_written_by_the_vector_cores_binds_anywhere():
    """L2.MOV.020's vector copy leaves no mover write on `x` or `y`."""
    assert compiled(machine("multimesh_v9t8"))["f001"] == {"line_ports": [], "split": 0}


def without_vector_copy(flaw: bool = True) -> dict:
    """v9t8 with no vector cores: L2.MOV.020 has none to copy on, so an
    unproven copy stays on the mover."""
    doc = machine("multimesh_v9t8")
    doc["machine"]["die"]["units"]["vc"] = []
    doc["machine"]["flaws"]["F-001"]["present"] = flaw
    return doc


def test_an_unproven_mover_copy_reaching_l1_is_split_into_proven_commands():
    """L1.MOV.019's backstop: the copy kept on the mover leaves L1 as 256
    one-word commands, none chaining."""
    got = compiled(without_vector_copy())
    assert "copy w(x" in got["l1"].replace(" + ", "+")
    assert got["f001"]["split"] == 256, got["f001"]
    assert compiled(without_vector_copy(flaw=False))["f001"]["split"] == 0


REGISTER_COUNT = """target ktpu.v9

fn regs.l1(x: f16[256, 16] @dram, y: f16[512, 16] @dram)
    r1 = set 256
    move
        copy w(x + 0, (r1, 32)) -> w(y + 0, (r1, 64))
"""


def test_an_unproven_write_with_a_register_count_is_refused_on_f001():
    """L1.MOV.019: a split needs the counts."""
    doc = machine("multimesh_v9t8")
    with pytest.raises(Exception, match="L1.MOV.019"):
        rs.compile(REGISTER_COUNT, "regs", json.dumps(doc), l1=True)
    doc["machine"]["flaws"]["F-001"]["present"] = False
    rs.compile(REGISTER_COUNT, "regs", json.dumps(doc), l1=True)


REGISTER_FIELDS = """target ktpu.v9

fn regs.l1(x: f16[256, 16] @dram, y: f16[512, 16] @dram)
    r1 = set {value}
    move
        copy {copy}
"""


def test_a_register_count_is_not_proven_by_its_placeholder():
    """L1.MOV.019: a contiguous copy of r1 = 129 words bursts 128 + 1; its
    count's placeholder 0 is an even run, but the command is refused."""
    doc = machine("multimesh_v9t8")
    text = REGISTER_FIELDS.format(
        value=129, copy="w(x + 0, (r1, 32)) -> w(y + 0, (r1, 32))"
    )
    with pytest.raises(Exception, match="L1.MOV.019"):
        rs.compile(text, "regs", json.dumps(doc), l1=True)


def test_a_register_write_base_is_known_to_a_word():
    """L1.MOV.019: y + r1 at r1 = 32 starts on an odd word; the placeholder's
    offset 0 proves nothing, and the split needs the base's parity. One word
    through a register base needs nothing."""
    doc = machine("multimesh_v9t8")
    text = REGISTER_FIELDS.format(
        value=32, copy="w(x + 0, (4, 32)) -> w(y + 0 + r1, (4, 32))"
    )
    with pytest.raises(Exception, match="L1.MOV.019"):
        rs.compile(text, "regs", json.dumps(doc), l1=True)
    one = REGISTER_FIELDS.format(
        value=32, copy="w(x + 0, (1, 32)) -> w(y + 0 + r1, (1, 32))"
    )
    assert not rs.compile(one, "regs", json.dumps(doc), l1=True)["verdict"]["refused"]
    doc["machine"]["flaws"]["F-001"]["present"] = False
    assert not rs.compile(text, "regs", json.dumps(doc), l1=True)["verdict"]["refused"]


INDEXED = """target ktpu.v9

fn rows.l1(x: f16[64, 48] @dram, i: i32[8] @dram, y: f16[16, 64] @dram)
    move
        {op}
"""


def test_a_split_gather_row_off_an_index_word_still_encodes():
    """L1.MOV.019 / ISA.MV.054: two three-word rows 128 bytes apart split into
    an even body and a lone tail a row; the second row's pieces read the list
    from its word, the first row masked. L1.VER.013 requires the index
    values used to resolve physical source addresses before certification."""
    doc = machine("multimesh_v9t8")
    op = "gather x + 0 -> w(y + 0, (2, 128), (3, 32)) index=i + 0 n=2 pitch=96 words=3"
    source = INDEXED.format(op=op).replace(
        "    move\n", "    store i <- 4294967296\n    move\n"
    )
    got = rs.compile(source, "rows", json.dumps(doc), l1=True)
    assert got["f001"]["split"] == 4, got["f001"]
    assert not got["verdict"]["refused"], got["verdict"]


def test_a_split_scatter_row_off_an_index_word_is_refused():
    """L1.MOV.019: a scatter row's write goes where its entry says, so no
    window masks the rows before it; a split row past the list's word refuses."""
    doc = machine("multimesh_v9t8")
    op = "scatter w(x + 0, (4, 32)) -> y + 0 index=i + 0 n=2 pitch=96 words=2"
    with pytest.raises(Exception, match="L1.MOV.019"):
        rs.compile(INDEXED.format(op=op), "rows", json.dumps(doc), l1=True)


def test_without_the_flaw_the_copy_stays_on_the_mover():
    doc = machine("multimesh_v9t8")
    doc["machine"]["flaws"]["F-001"]["present"] = False
    l1 = lowered(doc)
    assert "copy w(x+0, (256, 32)) -> w(y+0, (256, 64))" in l1.replace(" + ", "+")
    assert "vdrain" not in l1


def test_only_a_drain_proof_port_needs_cluster_burst_alignment():
    doc = machine("multimesh_v9t8")
    text = """target ktpu.v9
fn bursts.l1(x: mx7a[4, 32] @dram(gm=1, nk=1), w: mx7b[32, 32] @dram(gn=8, nk=1), y: f16[4, 32] @dram(tile=1x8))
    send mg[0]
        fill A0 <- x n=1
        fill B0 <- w n=8
        gemm 1x8x1 a=A0 b=B0
        drain y n=8
    barrier
"""
    got = rs.compile(text, "bursts", json.dumps(doc), l1=True)
    ports = got["f016"]["burst_ports"]
    burst = submission.cluster_burst(json.dumps(doc))
    binds = {"x": 32, "w": 64, "y": 4096 + 128}
    assert submission.burst_unaligned(binds, burst, ports) == ["y"]
    binds["y"] = 4096 + 256
    assert submission.burst_unaligned(binds, burst, ports) == []
    doc["machine"]["flaws"]["F-016"]["present"] = False
    binds["y"] = 4096 + 128
    assert (
        submission.burst_unaligned(
            binds, submission.cluster_burst(json.dumps(doc)), ports
        )
        == []
    )
