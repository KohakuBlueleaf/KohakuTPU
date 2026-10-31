"""The two tracers of a vector kernel, `dsl.ops` and `ktpu-kernels::trace`, give one
graph over operand kinds, expressions and shapes: same values, same types, same
refusals."""

import itertools
import json

import pytest

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.plan import emit
from kohakutpu.application.runtime.generated import rows_arguments
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.language.dsl import expr as E
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs

MACHINE = machine_of("multimesh_v9t6", nodes=1)
CORES = MACHINE.count("VC")
L0, L1, L2 = X.Load(0), X.Load(1), X.Load(2)
KINDS = ("full", "tile", "row", "col", "scalar")
SHAPES = list(itertools.product((4, 5, 20), (32, 64)))
#: Whole 128-value rows: Python's generator takes no others.
COUNTS = (128, 1024)
ONE = {"neg": X.op("neg", L0)}
TWO = {
    "add": X.op("add", L0, L1),
    "compare": X.op("where", X.op("cmplt", L0, L1), L0, L1),
    "after-constant": X.op("mul", X.op("add", L0, 1.0), L1),
}
THREE = {"fma": X.op("fma", L0, L1, L2)}
FOLDS = {
    1: {"sum": X.Reduce("sum", L0)},
    2: {"fold-of-mul": X.Reduce("sum", X.op("mul", L0, L1))},
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


def rust(case):
    if case[0] == "rows":
        name = "vector.rows_kernel"
        args = rows_arguments(*case[1:])
    else:
        _, e, dtypes, n, scalars = case
        name = "vector.flat_kernel"
        args = {"dtypes": dtypes, "n": n, "out_dtype": "f16", "scalars": scalars}
        args["e"] = emit.expr(e)
    source = rs._rs.generate(
        name, json.dumps(args), rs.machine_json(MACHINE), rs._MUTANT.get()
    )
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


def test_python_flat_takes_whole_rows():
    with pytest.raises(ValueError, match="whole 128-value rows"):
        V.flat_kernel(X.Load(0), ("f16",), 130, "f16", CORES)


def test_a_sum_opening_on_an_atom_keeps_its_brackets():
    a, b = E.var("a"), E.var("b")
    assert E.text(((a // 4) + b) // 3) == "(((a / 4) + b) / 3)"
