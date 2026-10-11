"""The language's stages: L3 -> L2 plans, L2 -> L2 passes and the L1
scheduler, each stage's text read back by the next reader. The target is the
v9 die's (`kohakutpu.compiler.target.TARGET`)."""

import random

import pytest
from kohakutpu.compiler.target import TARGET
from kohakutpu.language import kernels
from kohakutpu.language.l1.ops import MATH
from kohakutpu.language.l2 import nodes as L
from kohakutpu.language.l2 import reader
from kohakutpu.language.l2.verify import value
from kohakutpu.language.opt import schedule
from kohakutpu.language.pipeline import compile_l3, l2_text
from kohakutpu.language.text import module
from kohakutpu.language.text.syntax import parse


def load(name):
    return kernels.load(kernels.kernel(name))


@pytest.mark.parametrize("kernel", kernels.names())
def test_planned_l2_matches_the_hand_signature(kernel):
    m = load(kernel)
    planned = reader.body(module.read(compile_l3(m, kernel, TARGET)), kernel)
    hand = reader.body(m, kernel)
    assert [(p, t.dtype, t.shape) for p, t in planned.params] == [
        (p, t.dtype, t.shape) for p, t in hand.params
    ]


def _rows_touched(text, name):
    """Every (buffer, row) a row kernel's loads and stores touch, per trip."""
    b = reader.body(module.read(text), name)
    (par,) = b.stmts
    (loop,) = [s for s in par.body if isinstance(s, L.For)]
    out = set()
    for c in range(par.lo, par.hi):
        for t in range(loop.lo, loop.hi):
            env = {par.var: c, loop.var: t}
            for s in loop.body:
                if isinstance(s, (L.Load, L.Store)):
                    v = s.src if isinstance(s, L.Load) else s.dst
                    lo = value(v.axes[0].lo, env)
                    out |= {(v.name, r) for r in range(lo, lo + v.axes[0].size)}
    return out


@pytest.mark.parametrize("kernel", ["softmax", "layernorm"])
def test_retile_covers_the_same_rows(kernel):
    m = load(kernel)
    before = _rows_touched(l2_text(m, kernel, TARGET, ()), kernel)
    retiled = l2_text(m, kernel, TARGET, ("retile=8",))
    assert before == _rows_touched(retiled, kernel) and len(before) == 2 * 1024
    assert "+8, :" in retiled


def test_retile_auto_pipelines_only_a_second_reduction_chain():
    soft, norm = load("softmax"), load("layernorm")
    assert "+8, :" in l2_text(soft, "softmax", TARGET, ("retile=auto",))
    assert "+16, :" in l2_text(norm, "layernorm", TARGET, ("retile=auto",))


# ------------------------------------------------------------ the scheduler
def _run(stmts, terms):
    """Symbolic execution: each register chunk / predicate holds a term (an
    id, hash-consed in `terms`) of the op that wrote it and the terms it read."""
    state: dict = {}
    for st in stmts:
        op = schedule.Op(st, TARGET)
        key = (st.op, str(st.args), tuple(sorted((r, state.get(r)) for r in op.reads)))
        t = terms.setdefault(key, len(terms))
        for w in op.writes:
            state[w] = t
    return state


def _random_body(rng, n):
    lines = []
    for _ in range(n):
        d, a, b = (rng.randrange(6) for _ in range(3))
        kind = rng.randrange(4)
        if kind == 0:
            lines.append(f"vadd v{d}, v{a}, v{b}")
        elif kind == 1:
            lines.append(f"vmul v{d}[{rng.randrange(8)}:], v{a}, v{b} vl=16")
        elif kind == 2:
            lines.append(f"vmax v{d}, v{a}, merge(v{b}, 4)")
        else:
            lines.append(f"vexp2d v{d}, v{a}, bcast(v{b}[2], lane=1, sh=3)")
    return lines


@pytest.mark.parametrize("seed", range(20))
def test_scheduling_keeps_every_register_value(seed):
    stmts = parse("\n".join(_random_body(random.Random(seed), 40)))
    assert all(s.op in MATH for s in stmts)
    got = schedule.order(stmts, TARGET)
    assert sorted(map(id, got)) == sorted(map(id, stmts))
    terms: dict = {}
    assert _run(got, terms) == _run(stmts, terms)


def test_the_symbolic_check_sees_a_reordered_dependence():
    stmts = parse("vadd v0, v1, v2\nvmul v3, v0, v0\n")
    terms: dict = {}
    assert _run(stmts[::-1], terms) != _run(stmts, terms)


def test_scheduling_interleaves_independent_chains():
    body = ["vadd v0, v0, v1", "vadd v0, v0, v1", "vadd v2, v2, v3", "vadd v2, v2, v3"]
    got = [s.args[0].text for s in schedule.order(parse("\n".join(body)), TARGET)]
    assert got == ["v0", "v2", "v0", "v2"]
