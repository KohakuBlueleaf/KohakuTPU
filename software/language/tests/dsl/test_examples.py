"""Every DSL example traces at its sample shapes to L2 the reader takes, and
its trace-time constants change the text the way they say."""

import numpy as np
import pytest

import kohakutpu.language.dsl as kt
from kohakutpu.language import machine as MF
from kohakutpu.language.examples import dsl as ex
from kohakutpu.language.text import module as M

MACHINE = kt.Machine.of(MF.load("multimesh_v9t8"))


def first(e: ex.Example, **consts) -> kt.Traced:
    """The trace at each `kt.auto`'s first candidate (no compiler here)."""
    return e.kernel.trace(e.given, machine=MACHINE, **{**e.consts, **consts})


@pytest.mark.parametrize("name", sorted(ex.EXAMPLES))
def test_each_example_traces_to_l2(name):
    e = ex.EXAMPLES[name]
    tr = first(e)
    M.read(tr.text, f"<{name}>")
    assert set(tr.roles) == set(e.kernel.ports)
    assert tr.spec(e.out).dtype == "f16"


@pytest.mark.parametrize("name", sorted(ex.EXAMPLES))
def test_each_reference_runs_on_its_inputs(name):
    e = ex.EXAMPLES[name]
    ins = e.inputs(np.random.default_rng(0))
    out = e.reference(ins)
    assert out.shape == first(e).spec(e.out).shape and np.isfinite(out).all()


def test_the_activation_is_chosen_while_tracing():
    e = ex.EXAMPLES["activation"]
    relu = first(e, act="relu", scale=1.0).text
    silu = first(e, act="silu", scale=1.0).text
    assert "max v1, 0.0" in relu and "exp2" not in relu
    assert "exp2" in silu and "inv" in silu
    with pytest.raises(kt.DSLError, match="no activation 'swish'"):
        first(e, act="swish")


def test_layernorm_loads_its_weights_once_before_the_loop():
    tr = first(ex.EXAMPLES["layernorm"])
    lines = tr.text.splitlines()
    loop = next(i for i, ln in enumerate(lines) if ln.strip().startswith("for "))
    pre = [ln for ln in lines[:loop] if "load g" in ln or "load b" in ln]
    assert len(pre) == 2


def test_linear_refuses_a_shape_its_tiles_do_not_cover():
    e = ex.EXAMPLES["linear"]
    given = {
        "x": ((100, 256), "f16"),
        "w": ((512, 256), "f16"),
        "bias": ((512,), "f16"),
    }
    with pytest.raises(kt.DSLError, match="100 rows in whole tiles of 64"):
        e.kernel.trace(given, machine=MACHINE, tn=256, rows=8)


def test_linear_spreads_column_blocks_over_the_cores():
    e = ex.EXAMPLES["linear"]
    tr = e.kernel.trace(e.given, machine=MACHINE, act="none", tn=128, rows=8)
    assert "on vc[any]" in tr.text  # 4 column blocks x 2 cores, placed by the pass
    assert "par c1 in 0..8" in tr.text
