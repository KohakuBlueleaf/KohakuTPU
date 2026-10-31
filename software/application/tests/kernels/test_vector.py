"""Generated rows kernels as L2 text, without a card: every row a kernel
reads or writes is one of its R, and a value narrower than the output's
band (a statistic, a row broadcast) is stored across all of it."""

import re

import pytest

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.language.dsl.mutant import ROWS_OF_BANDS
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs

MACHINE = machine_of("multimesh_v9t8", nodes=1)


def _l2(e, operands, R, W, out) -> str:
    k, given, _ = V.rows_kernel(e, tuple(operands), R, W, out, 2)
    traced = k.trace(given, machine=K.dsl_machine(MACHINE))
    K.compile_traced(traced, MACHINE)  # the L2 compiler takes it
    return traced.text


def _rows(text: str, op: str, name: str) -> list:
    """``[(first row, rows)]`` of every `op` (load / store / copy) of a view
    of buffer `name`, at every value of the `par` and `for` variables around
    it (each range's bounds evaluated at the variables outside it)."""
    out, scopes = [], []  # scopes: [(indent, var, lo, hi)]
    for line in text.splitlines():
        indent = len(line) - len(line.lstrip())
        scopes = [s for s in scopes if s[0] < indent]
        head = re.match(r"\s*(?:par|for) (\w+) in (.+?)\.\.(.+?)(?: on |$)", line)
        if head:
            scopes.append((indent, *head.groups()))
            continue
        m = re.search(rf"{op} {name}\[([^:\]]+) : \+(\d+)", line)
        if op == "copy" and not m:  # a copy's source
            m = re.search(rf"copy .*<- {name}\[([^:\]]+) : \+(\d+)", line)
        if m:
            _each(scopes, m.group(1), int(m.group(2)), {}, out)
    return out


def _each(scopes: list, first: str, n: int, env: dict, out: list) -> None:
    """``(first row, n)`` at every value of `scopes`' variables (index
    arithmetic over them, evaluated)."""
    if not scopes:
        out.append((eval(first.replace("/", "//"), {}, env), n))
        return
    _, var, lo, hi = scopes[0]
    for v in range(
        eval(lo.replace("/", "//"), {}, env), eval(hi.replace("/", "//"), {}, env)
    ):
        _each(scopes[1:], first, n, {**env, var: v}, out)


@pytest.mark.parametrize("R", [1, 3, 5, 6, 17, 30, 63])
def test_no_row_past_R_is_read_or_written(R):
    e = X.op("add", X.Load(0), 1.0)
    text = _l2(
        e, [Operand("full", pitch=128, rows=R)], R, 128, Operand("full", pitch=128)
    )
    seen = {
        op: _rows(text, op, "in0") + _rows(text, op, "out")
        for op in ("load", "store", "copy")
    }
    # Every row of R is read and written, none past it.
    read = _rows(text, "load", "in0") + _rows(text, "copy", "in0")
    assert {r for f, n in read for r in range(f, f + n)} >= set(range(R))
    out = _rows(text, "store", "out") + _rows(text, "copy", "out")
    written = {r for f, n in out for r in range(f, f + n)}
    assert written == set(range(R))
    for op, views in seen.items():
        for first, n in views:
            assert first + n <= R, (op, first, n, R)
    if R % 4:
        # The last band, R % 4 rows of it real: through a scratch.
        assert f": +{R % 4}" in text


@pytest.mark.parametrize("deal", ["hchunk", "balanced"])
@pytest.mark.parametrize("vc", range(1, 5))
@pytest.mark.parametrize("n", range(34))
def test_split_covers_every_trip_once(vc, n, deal, monkeypatch):
    machine = machine_of(
        "multimesh_v9t8",
        nodes=1,
        units={**MACHINE.units, "VC": tuple((2, i + 1) for i in range(vc))},
    )
    monkeypatch.setattr(V, "TRIPS", deal)
    cores, start, trips = V.split(n, machine.count("VC"))
    seen = [start(c) + i for c in range(cores) for i in range(trips(c))]
    assert sorted(seen) == list(range(n))
    counts = [trips(c) for c in range(cores)]
    most = max(1, min(vc, n))
    if deal == "balanced":
        assert cores == most
        assert max(counts) - min(counts) <= 1
    else:
        h = -(-n // most)
        assert cores == (-(-n // h) if n else 1)
        assert all(t == h for t in counts[:-1])


@pytest.mark.parametrize("vc", range(1, 5))
def test_split_traces_every_row_once(vc):
    machine = machine_of(
        "multimesh_v9t8",
        nodes=1,
        units={**MACHINE.units, "VC": tuple((2, i + 1) for i in range(vc))},
    )
    rows = 208
    operand = Operand("full", pitch=32, rows=rows)
    kernel, given, _ = V.rows_kernel(
        X.Load(0), (operand,), rows, 32, operand, machine.count("VC"), bands=1
    )
    text = kernel.trace(given, machine=K.dsl_machine(machine)).text
    for op, name in (("load", "in0"), ("store", "out")):
        seen = [
            r
            for first, count in _rows(text, op, name)
            for r in range(first, first + count)
        ]
        assert sorted(seen) == list(range(rows))


def test_a_statistic_gives_a_row_broadcast_its_rows():
    """Against a `col` band, each row reads its own statistic value: one
    value of 16 rows, stored once, not a band tiled down them."""
    out = Operand("full", pitch=32, rows=32)
    text = _l2(
        X.op("add", X.Load(0), X.Load(1)),
        [Operand("row", rows=32), Operand("col")],
        32,
        32,
        out,
    )
    assert re.search(r"= add .*: f32\[16, 32\] @reg\(gt4\)", text)
    assert _rows(text, "store", "out") == [(0, 16), (16, 16)]
    k, given, _ = V.rows_kernel(
        X.op("add", X.Load(0), X.Load(1)),
        (Operand("row", rows=32), Operand("col")),
        32,
        32,
        out,
        3,
    )
    with rs.mutant(ROWS_OF_BANDS):
        mutant = k.trace(given, machine=K.dsl_machine(MACHINE)).text
    assert re.search(r"= add .*: f32\[4, 32\] @reg\(gt4\)", mutant)
    assert len(_rows(mutant, "store", "out")) == 8


def test_a_lane_gives_a_row_broadcast_no_rows():
    """A scalar is one value every row reads: its 16 lanes are not rows, so
    the col band stays a band."""
    text = _l2(
        X.op("add", X.Load(0), X.Load(1)),
        [Operand("scalar", rows=16), Operand("col")],
        8,
        32,
        Operand("full", pitch=32, rows=8),
    )
    assert re.search(r"= add .*: f32\[4, 32\] @reg\(gt4\)", text)
    assert not re.search(r"= add .*: f32\[16, 32\]", text)


def test_a_statistic_is_stored_across_the_row():
    text = _l2(
        X.Load(0), [Operand("row", rows=16)], 16, 128, Operand("full", pitch=128)
    )
    stores = [ln for ln in text.splitlines() if "store out" in ln]
    assert re.search(r"= mul \w+\[:, \*\], 1.0.*: f32\[16, 32\] @reg\(gt4\)", text)
    # Materialised register rows, packed into each of the row's four bands.
    assert sorted(re.findall(r", (\d+) : \+32\]", "\n".join(stores))) == [
        "0",
        "32",
        "64",
        "96",
    ]
