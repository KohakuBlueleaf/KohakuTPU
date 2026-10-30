"""The Rust kernels that make their own operands (`gen::prepared`) against
Python's.

- `prepared.rows_kernel`: the same graph, name and statements over made
  operands of one and two relayout steps, a twice-read made buffer
  (`alias`), plain operands beside made ones, statistic outputs with a tail
  band, and trips that split unevenly over the cores.
- `reductions.column_tree_kernel` with `made`: the same graph.
- `matmul.mm_kernel` with `made`: the same ports, specs and statements (the
  name carries knobs Python spells differently).
- `matmul.quant_kernel` with `prepared` (`native` and `staged`): the same
  ports and made-input statements; after them, Rust and Python agree on
  exactly the statements they agree on without `prepared`."""

import json

import pytest

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.kernels import prepared as P
from kohakutpu.application.kernels import relayout as RL
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.plan import emit
from kohakutpu.application.runtime import launch_reductions as LR
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.language import machine as MF
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs

BOARD = "multimesh_v9t6"
MACHINE = machine_of(BOARD, nodes=1)
CORES = MACHINE.count("VC")
HW = RL.hw_of(MF.load(BOARD), CORES)

SUM = X.Reduce("sum", X.Load(0))
TWICE = X.Op("mul", (X.Load(0), X.Const(2.0)))
PAIR = X.Op("add", (X.Load(0), X.Load(1)))


def transpose(rows, cols):
    """The [rows, cols] read of a [cols, rows] source."""
    n = rows * cols
    return (n, ((rows, 1), (cols, rows)), 0, ((rows, cols), (cols, 1)), 0, n)


def repitch(rows, width, pitch, at=0):
    """Rows of `width` values `width` apart, laid `pitch` apart."""
    n = at + rows * width
    return (
        n,
        ((rows, width), (width, 1)),
        at,
        ((rows, pitch), (width, 1)),
        0,
        rows * pitch,
    )


def full(pitch, offset=0, rows=0):
    return Operand("full", pitch=pitch, offset=offset, rows=rows)


#: name -> (e, operands, R, W, out, {operand: [step]}, alias)
CASES = {
    "transposed_sum": (
        SUM,
        (full(64),),
        40,
        64,
        Operand("row"),
        {0: [transpose(40, 64)]},
        {},
    ),
    "transposed_sum_tail": (
        SUM,
        (full(64),),
        21,
        64,
        Operand("row"),
        {0: [transpose(21, 64)]},
        {},
    ),
    "repitched_scaled": (
        TWICE,
        (full(64),),
        88,
        64,
        full(64),
        {0: [repitch(88, 40, 64)]},
        {},
    ),
    "repitched_misaligned_odd_rows": (
        TWICE,
        (full(96),),
        13,
        96,
        full(96),
        {0: [repitch(13, 84, 96, at=5)]},
        {},
    ),
    "two_steps": (
        TWICE,
        (full(64),),
        32,
        64,
        full(64),
        {0: [repitch(32, 40, 48), transpose(32, 48)]},
        {},
    ),
    "halves_of_one_buffer": (
        PAIR,
        (full(64), full(64, offset=16 * 64)),
        16,
        64,
        full(64),
        {0: [transpose(32, 64)]},
        {1: 0},
    ),
    "made_beside_plain": (
        X.Op("mul", (X.Load(0), X.Load(1))),
        (full(64), Operand("col")),
        24,
        64,
        full(64),
        {0: [transpose(24, 64)]},
        {},
    ),
    "plain_beside_made": (
        PAIR,
        (full(64), full(64)),
        24,
        64,
        full(64),
        {1: [transpose(24, 64)]},
        {},
    ),
}


def plan_args(step) -> dict:
    n, walk, at, dwalk, dat, m = step
    return {
        "src_n": n,
        "walk": [list(w) for w in walk],
        "at": at,
        "dwalk": [list(w) for w in dwalk],
        "dat": dat,
        "m": m,
        "keep": False,
    }


def bare(node):
    if isinstance(node, dict):
        return {k: bare(v) for k, v in node.items() if k != "where"}
    if isinstance(node, list):
        return [bare(v) for v in node]
    return node


def rust_graph(case) -> dict:
    e, ops, R, W, out, steps, alias = case
    call = {
        "e": emit.expr(e),
        "operands": [emit.operand(o) for o in ops],
        "R": R,
        "W": W,
        "out": emit.operand(out),
        "made": {
            str(i): [
                {"made_by": {"gen": "relayout.plan", "args": plan_args(s)}} for s in ss
            ]
            for i, ss in steps.items()
        },
        "alias": {str(i): j for i, j in alias.items()},
    }
    got = rs._rs.generate(
        "prepared.rows_kernel", json.dumps(call), rs.machine_json(MACHINE)
    )
    return bare(json.loads(got))


def python_graph(case) -> dict:
    e, ops, R, W, out, steps, alias = case
    made = {
        i: [RL.plan(*s[:4], s[4], s[5], False, HW) for s in ss]
        for i, ss in steps.items()
    }
    k, given = P.rows_kernel(e, ops, R, W, out, CORES, made, HW, alias)
    return bare(json.loads(k.trace(given, machine=K.dsl_machine(MACHINE)).graph_json))


@pytest.mark.parametrize("name", sorted(CASES))
def test_rust_kernel_is_python_kernel(name):
    assert rust_graph(CASES[name]) == python_graph(CASES[name])


#: name -> (e, operands, R, W, out, {operand: [step]}): `prepared_tree`'s
#: column trees, odd and even rows, a kept operand read from a made one.
TREES = {
    "tree_transposed": (SUM, (full(64),), 40, 64, full(64), {0: [transpose(40, 64)]}),
    "tree_odd_two_steps": (
        SUM,
        (full(64),),
        13,
        64,
        full(64),
        {0: [repitch(13, 40, 48), transpose(13, 48)]},
    ),
    "tree_kept_made": (
        X.Op("add", (X.Reduce("sum", X.Load(0)), X.Load(1))),
        (full(64), full(64)),
        7,
        64,
        full(64),
        {1: [transpose(7, 64)]},
    ),
}


def made_arg(steps) -> dict:
    return {
        str(i): [
            {"made_by": {"gen": "relayout.plan", "args": plan_args(s)}} for s in ss
        ]
        for i, ss in steps.items()
    }


@pytest.mark.parametrize("name", sorted(TREES))
def test_rust_tree_is_python_tree(name):
    e, ops, R, W, out, steps = TREES[name]
    call = {
        "e": emit.expr(e),
        "operands": [emit.operand(o) for o in ops],
        "R": R,
        "W": W,
        "out": emit.operand(out),
        "cores": CORES,
        "made": made_arg(steps),
    }
    got = rs._rs.generate(
        "reductions.column_tree_kernel", json.dumps(call), rs.machine_json(MACHINE)
    )
    made = {
        i: [RL.plan(*s[:4], s[4], s[5], False, HW) for s in ss]
        for i, ss in steps.items()
    }
    k, given = LR.column_tree_kernel(e, ops, R, W, out, CORES, made=made, hw=HW)
    want = json.loads(k.trace(given, machine=K.dsl_machine(MACHINE)).graph_json)
    assert bare(json.loads(got)) == bare(want)


def plans_of(steps) -> list:
    return [RL.plan(*s[:4], s[4], s[5], False, HW) for s in steps]


def traced(k, given) -> dict:
    return bare(json.loads(k.trace(given, machine=K.dsl_machine(MACHINE)).graph_json))


#: name -> (m, n, k, e, extras, {operand: [step]}): `prepared_mm`'s products.
PRODUCTS = {
    "mm_extra_transposed": (32, 64, 64, PAIR, (full(64),), {1: [transpose(32, 64)]}),
    "mm_extra_two_steps": (
        16,
        64,
        64,
        X.Op("mul", (X.Load(0), X.Load(1))),
        (full(64),),
        {1: [repitch(16, 40, 48), transpose(16, 48)]},
    ),
}


@pytest.mark.parametrize("name", sorted(PRODUCTS))
def test_rust_made_product_is_python_product(name):
    m, n, k, e, extras, steps = PRODUCTS[name]
    mm = MM.plan(1, m, n, k, True, 4, machine_json=rs.machine_json(MACHINE))
    out = Operand("full", pitch=mm.n, rows=mm.mp)
    plan = {"bt": 1, "m": m, "n": n, "k": k, "shared": True, "quantum": 4}
    call = {
        "mm": {"made_by": {"gen": "matmul.plan", "args": plan}},
        "e": emit.expr(e),
        "extras": [emit.operand(o) for o in extras],
        "out": emit.operand(out),
        "made": made_arg(steps),
    }
    got = json.loads(
        rs._rs.generate("matmul.mm_kernel", json.dumps(call), rs.machine_json(MACHINE))
    )
    made = {i: plans_of(ss) for i, ss in steps.items()}
    clusters = MACHINE.count("MG")
    want = traced(*MM.mm_kernel(mm, e, extras, out, clusters, CORES, made=made, hw=HW))
    del got["name"], want["name"]
    assert bare(got) == want


def quant_rust(rows, cols, rp, cp, steps, strategy) -> dict:
    call = {
        "src": emit.operand(full(cols)),
        "rows": rows,
        "cols": cols,
        "rp": rp,
        "cp": cp,
        "side": "a",
        "g": 2,
        "nk": 1,
        "batches": 1,
        "prepared": [
            {"made_by": {"gen": "relayout.plan", "args": plan_args(s)}} for s in steps
        ],
        "pad_strategy": strategy,
    }
    got = rs._rs.generate(
        "matmul.quant_kernel", json.dumps(call), rs.machine_json(MACHINE)
    )
    return bare(json.loads(got))


def quant_python(rows, cols, rp, cp, steps, strategy) -> dict:
    k = MM.quant_kernel(
        full(cols),
        rows,
        cols,
        rp,
        cp,
        "a",
        2,
        1,
        1,
        prepared=plans_of(steps),
        hw=HW,
        pad_strategy=strategy,
    )
    return traced(*k)


#: name -> (rows, cols, rp, cp, [step], pad strategy): `prepared_quant`'s
#: packings, unpadded and staged with padding rows.
PACKINGS = {
    "quant_native": (32, 64, 32, 64, [transpose(32, 64)], "native"),
    "quant_staged_padded": (30, 64, 32, 64, [transpose(30, 64)], "staged"),
}


@pytest.mark.parametrize("name", sorted(PACKINGS))
def test_rust_prepared_packing_is_python_packing(name):
    rows, cols, rp, cp, steps, strategy = PACKINGS[name]
    got = quant_rust(rows, cols, rp, cp, steps, strategy)
    want = quant_python(rows, cols, rp, cp, steps, strategy)
    plain_got = quant_rust(rows, cols, rp, cp, [], strategy)
    plain_want = quant_python(rows, cols, rp, cp, [], strategy)
    assert got["ports"] == want["ports"]
    made = len(want["body"]) - len(plain_want["body"])
    assert made > 0 and len(got["body"]) - len(plain_got["body"]) == made
    assert got["body"][:made] == want["body"][:made]
    agree = [a == b for a, b in zip(got["body"][made:], want["body"][made:])]
    assert agree == [a == b for a, b in zip(plain_got["body"], plain_want["body"])]


def test_cases_reach_every_branch():
    """Some case has two steps, an alias, a plain operand, a tail band, a
    statistic output, and a stage's trips not a multiple of the cores (there
    the two trip splits trace apart)."""
    uneven, tails = False, False
    for e, ops, R, W, out, steps, alias in CASES.values():
        stat = V.holds_stat(ops, out.kind)
        quantum = V.STAT_ROWS if stat else 4
        pl = V.plan(ops, out.kind, R, W, quantum=quantum)
        for sp, _, rows in V.stages(pl, R, quantum):
            tails |= rows is not None
            uneven |= rows is None and sp.nb > CORES > 1 and sp.nb % CORES != 0
    assert tails and uneven
    assert any(len(ss) > 1 for c in CASES.values() for ss in c[5].values())
    assert any(c[6] for c in CASES.values())
    assert any(len(c[1]) > len(c[5]) + len(c[6]) for c in CASES.values())
    assert any(c[4].kind == "row" for c in CASES.values())
