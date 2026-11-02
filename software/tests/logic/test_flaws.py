"""Silicon flaws: each machine file says which its image carries, and a handling
runs exactly where its flaw is present (docs/spec/stack/flaws.md)."""

import json

import pytest

from kohakutpu.language import examples
from kohakutpu.language.machine import MACHINES
from kohakutpu.stack import rs

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


def lowered(doc: dict) -> str:
    text = examples.example("mover/lone", "l2").read_text("utf-8")
    return rs.compile(text, "lone", json.dumps(doc))["l1"]


def test_a_copy_writing_one_word_runs_runs_on_the_vector_cores():
    l1 = lowered(machine("multimesh_v9t8"))
    assert "vdrain a1 <- l1[0]" in l1
    assert "desc a1=y+0 walk=(64, 256)" in l1
    assert "copy w(" not in l1


def test_without_the_flaw_the_copy_stays_on_the_mover():
    doc = machine("multimesh_v9t8")
    doc["machine"]["flaws"]["F-001"]["present"] = False
    l1 = lowered(doc)
    assert "copy w(x+0, (256, 32)) -> w(y+0, (256, 64))" in l1.replace(" + ", "+")
    assert "vdrain" not in l1
