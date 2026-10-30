"""The DSL tracer: what a kernel records, what it decides while tracing, and
what it refuses (each at the user's source line)."""

from dataclasses import replace

import numpy as np
import pytest

import kohakutpu.language.dsl as kt
from kohakutpu.language import machine as MF
from kohakutpu.language.dsl import expr as E
from kohakutpu.language.dsl.mutant import ATOM_SUM_SHORTCUT, MUTANT
from kohakutpu.language.text import module as M

MACHINE = kt.Machine.of(MF.load("multimesh_v9t8"))


@kt.kernel
def scale(x=kt.In("R", "W"), y=kt.Out("R", "W"), *, k=2.0, rows=8):
    R = x.shape[0]
    per = R // kt.machine().vc
    for c in kt.par(kt.machine().vc, on=kt.vc):
        for t in kt.range(per // rows):
            r = c * per + t * rows
            v = kt.load(x[r : r + rows, :], gt4=True)
            if k != 1.0:
                v = v * k
            kt.store(y[r : r + rows, :], v)


def body(text: str) -> list:
    return [ln.strip() for ln in text.splitlines()[3:] if ln.strip()]


def test_a_trace_is_l2_text_the_reader_takes():
    tr = scale.trace({"x": ((64, 128), "f16")}, machine=MACHINE)
    assert tr.text.startswith("target ktpu.v9")
    assert "fn scale.l2(x: f16[64, 128] @dram, y: f16[64, 128] @dram)" in tr.text
    M.read(tr.text, "<scale>")  # parses as an L2 module
    assert tr.roles == {"x": "in", "y": "out"}
    assert tr.spec("y").shape == (64, 128)


def test_a_loop_is_recorded_once_whatever_its_trip_count():
    small = scale.trace({"x": ((64, 128), "f16")}, machine=MACHINE)
    huge = scale.trace({"x": ((1 << 20, 128), "f16")}, machine=MACHINE)
    assert len(body(small.text)) == len(body(huge.text))
    assert "for t1 in 0..65536" in huge.text


def test_compile_time_branches_trace_away():
    with_mul = scale.trace({"x": ((64, 128), "f16")}, machine=MACHINE, k=3.0).text
    without = scale.trace({"x": ((64, 128), "f16")}, machine=MACHINE, k=1.0).text
    assert "mul" in with_mul and "mul" not in without


def test_a_store_of_a_value_not_the_views_shape_is_refused():
    @kt.kernel
    def bad(x=kt.In("R", 128), y=kt.Out("R", 128)):
        for c in kt.par(2, on=kt.vc):
            # A 32-column band, bytes as loaded, into a 128-column view.
            kt.store(y[0:4, :], kt.load(x[0:4, 0:32], gt4=True))

    with pytest.raises(
        kt.DSLError, match=r"a store of \[4, 32\] values into a \[4, 128\]"
    ):
        bad.trace({"x": ((8, 128), "f16")}, machine=MACHINE)


def test_a_branch_on_a_runtime_value_is_refused():
    @kt.kernel
    def bad(x=kt.In("R", 128), y=kt.Out("R", 128)):
        for c in kt.par(2, on=kt.vc):
            v = kt.load(x[0:4, :], gt4=True)
            # The checker flags this too (`Value.__bool__` is NoReturn); the test runs it.
            if v:  # pyright: ignore[reportGeneralTypeIssues]
                kt.store(y[0:4, :], v)

    with pytest.raises(kt.TracerControlFlowError):
        bad.trace({"x": ((8, 128), "f16")}, machine=MACHINE)


def test_a_branch_on_a_loop_variable_is_refused_and_when_is_the_way():
    @kt.kernel
    def bad(x=kt.In("R", 128), y=kt.Out("R", 128)):
        for c in kt.par(2, on=kt.vc):
            if c == 0:  # pyright: ignore[reportGeneralTypeIssues]
                kt.store(y[0:4, :], kt.load(x[0:4, :], gt4=True))

    with pytest.raises(kt.DSLError):
        bad.trace({"x": ((8, 128), "f16")}, machine=MACHINE)

    @kt.kernel
    def good(x=kt.In("R", 128), y=kt.Out("R", 128)):
        for c in kt.par(2, on=kt.vc):
            with kt.when(c == 0):
                kt.store(y[0:4, :], kt.load(x[0:4, :], gt4=True))

    assert "when c1 == 0" in good.trace({"x": ((8, 128), "f16")}, machine=MACHINE).text


def test_named_extents_bind_once():
    @kt.kernel
    def add(a=kt.In("R", "W"), b=kt.In("R", "W"), y=kt.Out("R", "W")):
        for c in kt.par(2, on=kt.vc):
            kt.store(
                y[0:4, :], kt.load(a[0:4, :], gt4=True) + kt.load(b[0:4, :], gt4=True)
            )

    with pytest.raises(kt.DSLError, match="extent W is 128 and 64"):
        add.trace({"a": ((8, 128), "f16"), "b": ((8, 64), "f16")}, machine=MACHINE)


def test_require_refuses_by_name():
    @kt.kernel
    def wide(x=kt.In("R", "W"), y=kt.Out("R", "W")):
        kt.require(x.shape[1] <= 256, f"rows up to 256, not {x.shape[1]}")
        for c in kt.par(2, on=kt.vc):
            kt.store(y[0:4, :], kt.load(x[0:4, :], gt4=True))

    with pytest.raises(kt.DSLError, match="rows up to 256, not 512"):
        wide.trace({"x": ((8, 512), "f16")}, machine=MACHINE)


def test_auto_takes_the_first_candidate_the_check_passes():
    seen = []

    def check(tr):
        seen.append(tr.consts["rows"])
        if tr.consts["rows"] > 8:
            raise ValueError("too many rows for the L1")

    tr = scale.trace(
        {"x": ((64, 128), "f16")},
        machine=MACHINE,
        check=check,
        rows=kt.auto(32, 16, 8, 4),
    )
    assert tr.consts["rows"] == 8 and seen == [32, 16, 8]


def test_auto_reports_every_candidate_when_none_passes():
    def never(tr):
        raise ValueError(f"no {tr.consts['rows']}")

    with pytest.raises(kt.DSLError, match="no 16") as e:
        scale.trace(
            {"x": ((64, 128), "f16")}, machine=MACHINE, check=never, rows=kt.auto(16, 8)
        )
    assert "no 8" in str(e.value)


def test_auto_names_the_distinct_causes_with_counts():
    def stale(tr):
        if tr.consts["rows"] == 4:
            raise kt.DSLError("4 rows do not fill a band")
        raise ValueError("unknown field `acc_bits`\nat line 9")

    with pytest.raises(kt.DSLError) as e:
        scale.trace(
            {"x": ((64, 128), "f16")},
            machine=MACHINE,
            check=stale,
            rows=kt.auto(32, 16, 8, 4),
        )
    head = str(e.value).splitlines()[0]
    assert head.endswith(
        "(4 candidates: 3 x ValueError: unknown field `acc_bits`; "
        "1 x DSLError: 4 rows do not fill a band):"
    ), head


def test_a_trace_is_cached_per_shape_and_constants():
    a = scale.trace({"x": ((64, 128), "f16")}, machine=MACHINE, k=5.0)
    assert scale.trace({"x": ((64, 128), "f16")}, machine=MACHINE, k=5.0) is a
    assert scale.trace({"x": ((128, 128), "f16")}, machine=MACHINE, k=5.0) is not a


def test_a_trace_needs_a_machine():
    with pytest.raises(kt.DSLError, match="needs the machine"):
        scale.trace({"x": ((64, 128), "f16")})


def test_index_arithmetic_is_affine_text():
    c, t = E.var("c"), E.var("t")
    assert E.text(c * 128 + t * 16) == "c*128 + t*16"
    # A negative term never leads (the L2 reader would take `-(...)` apart).
    assert E.text(-(c % 2) + 2) == "2 - (c % 2)"
    assert E.evaluate(c * 128 + t * 16, {"c": 1, "t": 3}) == 176


ENVS = [{"k": 0, "t": 1, "u": 2}, {"k": 13, "t": 4, "u": 7}, {"k": 29, "t": 9, "u": 3}]


def nested(rng, depth):
    """1 to 3 terms of a variable or a `//`, `%`, `*` over smaller indices
    (int divisors 2..8), coefficients -3..3, plus a constant."""
    out = int(rng.integers(-4, 5))
    for _ in range(int(rng.integers(1, 4))):
        op = 0 if depth == 0 else int(rng.integers(0, 4))
        if op == 0:
            a = E.var("ktu"[int(rng.integers(0, 3))])
        elif op == 1:
            a = nested(rng, depth - 1) // int(rng.integers(2, 9))
        elif op == 2:
            a = nested(rng, depth - 1) % int(rng.integers(2, 9))
        else:
            a = nested(rng, depth - 1) * nested(rng, depth - 1)
        out = out + a * int(rng.choice([1, 1, 2, 3, -1, -2, -3]))
    return out


def misread():
    """The generated indices whose text, read with L2's precedence (Python's
    `//`, `%`, `*` share one left-associative level above `+ -`, as L2's
    do), is not their value; and how many were compared."""
    rng = np.random.default_rng(9)
    xs = [nested(rng, 3) for _ in range(400)]
    bad = [
        E.text(x)
        for x in xs
        if any(
            eval(E.text(x).replace("/", "//"), {}, dict(env)) != E.evaluate(x, env)
            for env in ENVS
        )
    ]
    return bad, len(xs)


def test_index_text_reads_back_to_its_value():
    bad, n = misread()
    assert not bad, (len(bad), n, bad[:5])
    k, t = E.var("k"), E.var("t")
    assert E.text((k // 7 + t) % 2 * 200) == "(((k / 7) + t) % 2)*200"


def test_the_atom_sum_shortcut_misreads():
    token = MUTANT.set(ATOM_SUM_SHORTCUT)
    try:
        bad, n = misread()
        k, t = E.var("k"), E.var("t")
        spelt = E.text((k // 7 + t) % 2 * 200)
    finally:
        MUTANT.reset(token)
    assert bad, f"the shortcut misread none of {n}"
    assert spelt == "((k / 7) + t % 2)*200"


def test_a_symbolic_index_is_no_python_int():
    @kt.kernel
    def bad(x=kt.In("R", 128), y=kt.Out("R", 128)):
        for c in kt.par(2, on=kt.vc):
            for _ in range(int(c)):
                pass

    with pytest.raises(kt.DSLError):
        bad.trace({"x": ((8, 128), "f16")}, machine=MACHINE)


def test_the_machine_is_a_trace_time_constant():
    one = scale.trace({"x": ((64, 128), "f16")}, machine=replace(MACHINE, vc=1))
    two = scale.trace({"x": ((64, 128), "f16")}, machine=replace(MACHINE, vc=2))
    assert "par c1 in 0..1" in one.text and "par c1 in 0..2" in two.text
