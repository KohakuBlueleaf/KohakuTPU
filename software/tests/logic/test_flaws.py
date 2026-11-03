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


def machine(name: str) -> dict:
    return json.loads((MACHINES / f"{name}.json").read_text("utf-8"))


@pytest.mark.parametrize(
    "name",
    ["multimesh_v9", "multimesh_v9t4", "multimesh_v9t5", "multimesh_v9t8"],
)
def test_presence_follows_the_image(name):
    flaws = machine(name)["machine"]["flaws"]
    assert flaws["F-001"]["present"] == (name in SKIDDED)
    assert all(flaws[f]["present"] for f in flaws if f != "F-001")


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


def lowered(doc: dict, name: str = "lone") -> str:
    text = examples.example(f"mover/{name}", "l2").read_text("utf-8")
    return rs.compile(text, name, json.dumps(doc))["l1"]


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


def test_a_bind_off_the_write_line_is_refused_only_with_f001():
    """L2.MOV.020's proof assumes every base a whole DRAM write line."""
    doc = machine("multimesh_v9t8")
    line = submission.burst_line(json.dumps(doc))
    assert line == doc["machine"]["card"]["dram"]["line_bytes"] == 64
    assert submission.unaligned({"x": 256, "y": 4160, "z": 96}, line) == ["z"]
    doc["machine"]["flaws"]["F-001"]["present"] = False
    assert submission.unaligned({"z": 96}, submission.burst_line(json.dumps(doc))) == []


def test_without_the_flaw_the_copy_stays_on_the_mover():
    doc = machine("multimesh_v9t8")
    doc["machine"]["flaws"]["F-001"]["present"] = False
    l1 = lowered(doc)
    assert "copy w(x+0, (256, 32)) -> w(y+0, (256, 64))" in l1.replace(" + ", "+")
    assert "vdrain" not in l1
