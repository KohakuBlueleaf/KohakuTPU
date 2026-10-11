"""The `.ktpu` compiler stack: L3 -> L2 plans, L2 -> L2 passes, L2 -> L1
lowerings and the L1 scheduler, each stage's text read back by the next
reader. Numerics are the card runs' (`scripts/py/ktpu/run.py --level`)."""

import random

import pytest
from kohakuaccel.text import module
from kohakuaccel.text.syntax import parse
from kohakutpu.ir.l1.model import L1Model
from kohakutpu.ktpu.compile import compile_l1, compile_l3, l2_text
from kohakutpu.ktpu.l1 import node, schedule
from kohakutpu.ktpu.l1.vector import MATH
from kohakutpu.ktpu.l2 import nodes as L
from kohakutpu.ktpu.l2 import reader
from kohakutpu.ktpu.l2.verify import value

from kohakutpu import ktpu

MACHINE = L1Model().machine
KERNELS = ["silu", "add", "softmax", "layernorm", "mlp", "swiglu", "attention", "flash"]


def _norm(step):
    """A program step with its unit-op objects as their fields."""
    return tuple(
        (
            tuple(sorted(vars(e).items()))
            if hasattr(e, "__dict__")
            else (_norm(e) if isinstance(e, tuple) else e)
        )
        for e in step
    )


@pytest.mark.parametrize("level", ["l2", "l3"])
def test_compiled_flash_is_the_hand_program(level):
    m = ktpu.load(ktpu.kernel("flash"))
    got = module.read(compile_l1(m, "flash", level))
    mine = node.program(got, "flash", addresses(got, "flash"), MACHINE)
    hand = node.program(m, "flash", addresses(m, "flash"), MACHINE)
    assert [_norm(s) for s in mine.steps] == [_norm(s) for s in hand.steps]


def addresses(m, name):
    """Each L1 parameter bound to its own 16 MB window, for building."""
    return {p: (i + 1) << 24 for i, (p, _) in enumerate(m.body(name, "l1").params)}


@pytest.mark.parametrize("kernel", KERNELS)
@pytest.mark.parametrize("level", ["l2", "l3"])
def test_compiled_l1_builds(kernel, level):
    m = ktpu.load(ktpu.kernel(kernel))
    text = compile_l1(m, kernel, level)
    got = module.read(text)
    prog = node.program(got, kernel, addresses(got, kernel), MACHINE)
    assert prog.steps[-1] == ("barrier",)
    # Same parameters, in order, as the hand L1 body: the runner binds by position.
    hand = [p for p, _ in m.body(kernel, "l1").params]
    mine = [p for p, _ in got.body(kernel, "l1").params]
    n_in = len(m.body(kernel, "l3").params) + 1
    assert mine[:n_in] == hand[:n_in]


@pytest.mark.parametrize("kernel", KERNELS)
def test_planned_l2_matches_the_hand_signature(kernel):
    m = ktpu.load(ktpu.kernel(kernel))
    planned = reader.body(module.read(compile_l3(m, kernel)), kernel)
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
    m = ktpu.load(ktpu.kernel(kernel))
    before = _rows_touched(l2_text(m, kernel, ()), kernel)
    after = _rows_touched(l2_text(m, kernel, ("retile=8",)), kernel)
    assert before == after and len(before) == 2 * 1024
    assert "+8, :" in l2_text(m, kernel, ("retile=8",))


def test_retile_auto_pipelines_only_a_second_reduction_chain():
    soft = ktpu.load(ktpu.kernel("softmax"))
    norm = ktpu.load(ktpu.kernel("layernorm"))
    assert "+8, :" in l2_text(soft, "softmax", ("retile=auto",))
    assert "+16, :" in l2_text(norm, "layernorm", ("retile=auto",))


# ------------------------------------------------------------ the scheduler
def _run(stmts, terms):
    """Symbolic execution: each register chunk / predicate holds a term (an
    id, hash-consed in `terms`) of the op that wrote it and the terms it read."""
    state: dict = {}
    for st in stmts:
        op = schedule.Op(st)
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
    rng = random.Random(seed)
    lines = _random_body(rng, 40)
    stmts = parse("\n".join(lines))
    assert all(s.op in MATH for s in stmts)
    got = schedule.order(stmts)
    assert sorted(map(id, got)) == sorted(map(id, stmts))
    terms: dict = {}
    assert _run(got, terms) == _run(stmts, terms)


def test_the_symbolic_check_sees_a_reordered_dependence():
    stmts = parse("vadd v0, v1, v2\nvmul v3, v0, v0\n")
    terms: dict = {}
    assert _run(stmts[::-1], terms) != _run(stmts, terms)


def test_scheduling_interleaves_independent_chains():
    body = ["vadd v0, v0, v1", "vadd v0, v0, v1", "vadd v2, v2, v3", "vadd v2, v2, v3"]
    got = [s.args[0].text for s in schedule.order(parse("\n".join(body)))]
    assert got == ["v0", "v2", "v0", "v2"]
