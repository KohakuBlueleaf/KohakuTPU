"""Pending reads composed on their loads and packs (`forms.COMPOSED`,
L2.READ.001), against numpy and the materialized run of the same expression.

Offline: which reads compose. On the card model: the values.

    KOHAKUTPU_BOARD=multimesh_v9t6 KOHAKUTPU_SIM_BUILD=build/v9t6/vlt_card_v9t6_1n pytest ...
"""

import numpy as np
import pytest

from application.tests.ktpugrad.test_prepared import CASES, close, f
from kohakutpu.application import api as kp
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime import generated


def launches(monkeypatch, make, mode) -> list:
    """Generators of every composed launch of `make` on an offline device."""
    monkeypatch.setattr(FM, "COMPOSED", mode)
    seen = []
    launch = generated.launch

    def spied(dev, generator, arguments, bindings, kind="dsl", info=None):
        if arguments.get("composed"):
            seen.append(generator)
        return launch(dev, generator, arguments, bindings, kind=kind, info=info)

    monkeypatch.setattr(generated, "launch", spied)
    dev = D.Device(kind="offline", board="multimesh_v9t6")
    with D.attached(dev):
        make().realize()
        dev.drain()
    return seen


#: `tree.column` reads materialized: no composed consumer there.
COMPOSING = sorted(set(CASES) - {"rows_down_sum"})


@pytest.mark.parametrize("name", COMPOSING)
def test_on_composes_and_off_does_not(monkeypatch, name):
    make = CASES[name][0]
    on = launches(monkeypatch, make, "on")
    assert on
    assert not launches(monkeypatch, make, "off")
    assert set(launches(monkeypatch, make, "cost")) <= set(on)


@pytest.mark.parametrize("name", ["matmul_a_transposed", "matmul_b_transposed"])
def test_a_composed_contraction_launches_once(monkeypatch, name):
    runs = []
    launch = generated.launch

    def spied(dev, generator, arguments, bindings, kind="dsl", info=None):
        if generator == "matmul.mm_kernel":
            runs.append(bool(arguments.get("composed")))
        return launch(dev, generator, arguments, bindings, kind=kind, info=info)

    monkeypatch.setattr(FM, "COMPOSED", "on")
    monkeypatch.setattr(generated, "launch", spied)
    with D.attached(D.Device(kind="offline", board="multimesh_v9t6")) as dev:
        CASES[name][0]().realize()
        dev.drain()
    assert runs == [True]


def test_the_default_is_the_rust_decision():
    assert FM.COMPOSED == FM._PREPARED_DEFAULTS["composed"] == "on"


def test_a_repeated_read_is_decided_once(monkeypatch):
    """The decision compiles and costs the kernel: a step planned again asks
    the compiler nothing."""
    FM._decided.cache_clear()
    asked = []
    real = FM._rs.read_composed

    def read_composed(*args):
        asked.append(args)
        return real(*args)

    monkeypatch.setattr(FM._rs, "read_composed", read_composed)
    make = CASES["rows_along_sum"][0]
    launches(monkeypatch, make, "on")
    once = len(asked)
    launches(monkeypatch, make, "on")
    assert once and len(asked) == once


def test_a_kernel_a_later_stage_refuses_reads_materialized():
    """A rows kernel past `VLOOP.body`: its `order` stage refuses it composed,
    so the decision reads it materialized and the softmax runs."""
    a = np.random.default_rng(17).standard_normal((197, 40)).astype(np.float16)
    with D.attached(D.Device(kind="offline", board="multimesh_v9t6")) as dev:
        kp.tensor(a).softmax().realize()
        assert dev.runs


@pytest.mark.parametrize("name", sorted(CASES))
def test_composed_on_the_card(dev, monkeypatch, name):
    make, want, mx7 = CASES[name]
    budget = 0.03 if mx7 else 2e-3
    monkeypatch.setattr(FM, "COMPOSED", "on")
    got = make().numpy()
    monkeypatch.setattr(FM, "COMPOSED", "off")
    alone = make().numpy()
    w = want()
    assert close(got, w) < budget, np.abs(f(got) - w).max()
    assert close(got, alone) < budget
