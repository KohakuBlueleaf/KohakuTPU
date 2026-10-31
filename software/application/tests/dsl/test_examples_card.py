"""Every DSL example on the Verilated card through the runtime (`runtime.call`:
inputs uploaded, MXFP7 ports packed on the card), against its numpy
reference."""

from dataclasses import replace

import numpy as np
import pytest

from kohakutpu.application.runtime.call import call
from kohakutpu.language.examples import dsl as ex


@pytest.mark.parametrize("name", sorted(ex.EXAMPLES))
def test_example_on_the_card(dev, rel, name):
    e = ex.EXAMPLES[name]
    ins = e.inputs(np.random.default_rng(5))
    outs, run = call(dev, e.kernel, ins, **e.consts)
    assert rel(outs[e.out], e.reference(ins)) < e.tol
    assert run.cycles > 0


def test_splitk_sweeps_more_tiles_than_clusters(dev, rel):
    e = replace(ex.EXAMPLES["splitk"], shapes={"x": (64, 128), "w": (512, 128)})
    ins = e.inputs(np.random.default_rng(5))
    outs, _ = call(dev, e.kernel, ins, **e.consts)
    assert rel(outs[e.out], e.reference(ins)) < e.tol
