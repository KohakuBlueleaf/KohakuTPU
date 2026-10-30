"""The Rust single-package reduction generators (`reductions.*`) against
Python's emitters (`runtime.launch_reductions`), unmutated and under each
emitter mutant, over shapes reaching every fold and tree branch."""

import json

import pytest

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.plan import emit
from kohakutpu.application.runtime import launch_reductions as LR
from kohakutpu.application.runtime import rows as RW
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import rs

MACHINE = machine_of("multimesh_v9t6", nodes=1)

#: Python's mutant mode -> the Rust mutant of the same emitter fault.
MUTANTS = {
    "drop_member": "reductions/drop_member",
    "wrong_order": "reductions/wrong_order",
    "alias_members": "reductions/alias_members",
}

SUM = X.Reduce("sum", X.Load(0))
MASKED = X.Reduce("sum", X.Op("where", (X.Load(1), X.Load(0), X.Const(0.0))))
SCALED_MAX = X.Op("mul", (X.Reduce("max", X.Load(0)), X.Const(0.5)))
ROW_SCALED = X.Reduce("sum", X.Op("mul", (X.Load(0), X.Load(1))))
KEPT = X.Op("add", (X.Reduce("sum", X.Load(0)), X.Load(1)))


def full(w):
    return Operand("full", pitch=w)


#: (generator, e, operands, R, W, out)
CASES = [
    ("wide_fold_kernel", SUM, (full(1184),), 8, 1184, Operand("row")),
    ("wide_fold_kernel", SUM, (full(2368),), 3, 2368, Operand("row")),
    ("wide_fold_kernel", MASKED, (full(1216), Operand("col")), 8, 1216, Operand("row")),
    ("wide_fold_kernel", SCALED_MAX, (full(3552),), 20, 3552, Operand("row")),
    ("column_tree_kernel", SUM, (full(64),), 2, 64, full(64)),
    ("column_tree_kernel", SUM, (full(64),), 5, 64, full(64)),
    ("column_tree_kernel", SCALED_MAX, (full(128),), 13, 128, full(128)),
    ("column_tree_kernel", ROW_SCALED, (full(64), Operand("row")), 6, 64, full(64)),
    ("column_tree_kernel", ROW_SCALED, (full(64), Operand("row")), 32, 64, full(64)),
    ("column_tree_kernel", KEPT, (full(64), Operand("col")), 7, 64, full(64)),
]


def bare(node):
    if isinstance(node, dict):
        return {k: bare(v) for k, v in node.items() if k != "where"}
    if isinstance(node, list):
        return [bare(v) for v in node]
    return node


def call(e, operands, R, W, out):
    return {
        "e": emit.expr(e),
        "operands": [emit.operand(o) for o in operands],
        "R": R,
        "W": W,
        "out": emit.operand(out),
        "cores": MACHINE.count("VC"),
    }


def rust(gen, args, mutant=None):
    return bare(
        json.loads(
            rs._rs.generate(
                f"reductions.{gen}",
                json.dumps(args),
                rs.machine_json(MACHINE),
                mutant,
            )
        )
    )


def python(gen, args):
    return bare(LR.call_graph(f"reductions.{gen}", args, MACHINE))


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c[0]}-R{c[3]}-W{c[4]}")
def test_generator_is_python_emitter(case):
    gen, e, operands, R, W, out = case
    args = call(e, operands, R, W, out)
    assert rust(gen, args) == python(gen, args)


@pytest.mark.parametrize("mode", sorted(MUTANTS))
def test_mutant_is_python_mutant(mode, monkeypatch):
    moved = 0
    for gen, e, operands, R, W, out in CASES:
        args = call(e, operands, R, W, out)
        plain = rust(gen, args)
        got = rust(gen, args, MUTANTS[mode])
        monkeypatch.setattr(RW, "MUTANT", mode)
        assert got == python(gen, args), (gen, R, W)
        monkeypatch.setattr(RW, "MUTANT", None)
        moved += got["body"] != plain["body"]
    assert moved, f"{mode} changes no generated kernel body"
