"""Generated rows kernels as L2 text, without a card: every row a kernel
reads or writes is one of its R, and a value narrower than the output's
band (a statistic, a row broadcast) is stored across all of it."""

import re

import pytest

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.compiler import kernel as K
from kohakutpu.compiler import target as T

MACHINE = T.machine(nodes=1)


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
        out.append((eval(first, {}, env), n))
        return
    _, var, lo, hi = scopes[0]
    for v in range(eval(lo, {}, env), eval(hi, {}, env)):
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


def test_a_statistic_is_stored_across_the_row():
    text = _l2(X.Load(0), [Operand("row", rows=8)], 8, 128, Operand("full", pitch=128))
    stores = [ln for ln in text.splitlines() if "store out" in ln]
    # One packed band of 32 columns, stored into each of the row's four.
    assert sorted(re.findall(r", (\d+) : \+32\]", "\n".join(stores))) == [
        "0",
        "32",
        "64",
        "96",
    ]
