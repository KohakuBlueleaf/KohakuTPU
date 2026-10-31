"""The two tracers of a vector kernel, `dsl.ops` and `ktpu-kernels::trace`,
give one graph over operand kinds, expressions and shapes: same values, same
types, same refusals. A result-type rule held by one tracer alone fails here."""

import itertools
import json

import pytest

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.plan import emit
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.language.dsl.mutant import ROWS_OF_BANDS
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs

MACHINE = machine_of("multimesh_v9t6", nodes=1)
CORES = MACHINE.count("VC")
L0, L1, L2 = X.Load(0), X.Load(1), X.Load(2)
KINDS = ("full", "tile", "row", "col", "scalar")
SHAPES = list(itertools.product((4, 5, 8, 16, 20, 32), (32, 64)))
#: Whole 128-value rows: a tail is a generator's own stage, not a type rule.
COUNTS = (128, 256, 1024)
ONE = {"neg": X.op("neg", L0)}
TWO = {
    "add": X.op("add", L0, L1),
    "mul": X.op("mul", L0, L1),
    "max": X.op("max", L0, L1),
    "compare": X.op("where", X.op("cmplt", L0, L1), L0, L1),
    "chain": X.op("sub", X.op("add", L0, L1), L1),
    "after-constant": X.op("mul", X.op("add", L0, 1.0), L1),
}
THREE = {"fma": X.op("fma", L0, L1, L2), "where": X.op("where", L0, L1, L2)}
FOLDS = {
    1: {"sum": X.Reduce("sum", L0), "max": X.Reduce("max", L0)},
    2: {
        "fold-add": X.op("add", X.Reduce("sum", L0), L1),
        "fold-of-mul": X.Reduce("sum", X.op("mul", L0, L1)),
        "sub-fold": X.op("sub", L0, X.Reduce("max", L1)),
    },
    3: {},
}
BY_ARITY = {1: ONE, 2: TWO, 3: THREE}


def operand(kind, W):
    if kind in ("full", "tile"):
        return Operand(kind, pitch=W)
    return Operand("row", rows=64) if kind == "row" else Operand(kind)


def rows_cases(R, W):
    outs = (Operand("full", pitch=W), Operand("row"))
    for arity, table in BY_ARITY.items():
        exprs = (*table.values(), *FOLDS[arity].values())
        for e, kinds, out in itertools.product(
            exprs, itertools.product(KINDS, repeat=arity), outs
        ):
            yield "rows", e, tuple(operand(k, W) for k in kinds), R, W, out


def flat_cases(n):
    for arity, table in BY_ARITY.items():
        for e, lanes in itertools.product(
            table.values(), itertools.product((False, True), repeat=arity)
        ):
            scalars = tuple((i, 0) for i, lane in enumerate(lanes) if lane)
            yield "flat", e, ("f16",) * arity, n, scalars


def bare(node):
    if isinstance(node, dict):
        return {k: bare(v) for k, v in node.items() if k != "where"}
    if isinstance(node, list):
        return [bare(v) for v in node]
    return node


def rust(case, mutant=None):
    if case[0] == "rows":
        _, e, operands, R, W, out = case
        name = "vector.rows_kernel"
        args = {
            "operands": [emit.operand(o) for o in operands],
            "R": R,
            "W": W,
            "out": emit.operand(out),
        }
    else:
        _, e, dtypes, n, scalars = case
        name = "vector.flat_kernel"
        args = {"dtypes": dtypes, "n": n, "out_dtype": "f16", "scalars": scalars}
    args["e"] = emit.expr(e)
    source = rs._rs.generate(name, json.dumps(args), rs.machine_json(MACHINE), mutant)
    return bare(json.loads(source))


def python(case):
    if case[0] == "rows":
        k, given, _ = V.rows_kernel(*case[1:], CORES)
    else:
        _, e, dtypes, n, scalars = case
        k, given = V.flat_kernel(e, dtypes, n, "f16", CORES, scalars)
    return bare(json.loads(k.trace(given, machine=K.dsl_machine(MACHINE)).graph_json))


def outcome(trace, *args):
    """A graph, or None for a refusal: the tracers word theirs apart."""
    try:
        return trace(*args)
    except ValueError:
        return None


def all_cases():
    for R, W in SHAPES:
        yield from rows_cases(R, W)
    for n in COUNTS:
        yield from flat_cases(n)


@pytest.mark.parametrize("R,W", SHAPES)
def test_both_tracers_give_one_rows_graph(R, W):
    traced = 0
    for case in rows_cases(R, W):
        got = outcome(python, case)
        assert got == outcome(rust, case), case
        traced += got is not None
    assert traced


@pytest.mark.parametrize("n", COUNTS)
def test_both_tracers_give_one_flat_graph(n):
    traced = 0
    for case in flat_cases(n):
        got = outcome(python, case)
        assert got == outcome(rust, case), case
        traced += got is not None
    assert traced


def moved(rust_mutant=None):
    return sum(
        outcome(python, case) != outcome(rust, case, rust_mutant)
        for case in all_cases()
    )


def test_a_rule_only_in_python_is_seen():
    with rs.mutant(ROWS_OF_BANDS):
        assert moved()


@pytest.mark.parametrize("mutant", ["dsl.ops/lane-lost", "dsl.ops/gt4_off"])
def test_a_rule_only_in_rust_is_seen(mutant):
    assert moved(mutant)
