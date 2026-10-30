"""A full launch recipe distinguishes layouts and aliases, not buffer contents."""

from types import SimpleNamespace

import pytest

from kohakutpu.application.ktpugrad.recipe import signature
from kohakutpu.application.runtime.storage import FLAT, Form
from kohakutpu.compiler import _rs
from kohakutpu.stack import rs


def storage(address):
    return SimpleNamespace(
        addr=address,
        nbytes=256,
        dtype=None,
        owned=False,
        host=None,
        host_f16_dtype=None,
        forms={FLAT: Form(FLAT, address, 256, False)},
        valid={FLAT},
        zeroed=set(),
        site=None,
    )


def alias_witness(key):
    plan = SimpleNamespace(ast=object())
    a, b = storage(0x100000000), storage(0x100000000)
    aliased = key(plan, {0: a, 1: b})
    b.addr = 0x100001000
    b.forms[FLAT].addr = b.addr
    separate = key(plan, {0: a, 1: b})
    assert aliased != separate, "distinct wrappers sharing bytes need an alias key"
    a.addr += 0x10000
    b.addr += 0x10000
    a.forms[FLAT].addr = a.addr
    b.forms[FLAT].addr = b.addr
    assert separate == key(plan, {0: a, 1: b})
    b.forms[("pad", 16, 32)] = Form(("pad", 16, 32), b.addr + 512, 512, True)
    b.valid = {("pad", 16, 32)}
    assert separate != key(plan, {0: a, 1: b})


def test_plan_recipe_layout_and_allocation_alias_are_exact():
    alias_witness(signature)


def test_native_table_layout_owner_epoch_and_bound():
    epoch = _rs.RecipeEpoch()
    dev = SimpleNamespace(
        recipe_epoch=epoch,
        closed=False,
        recording=False,
        _healthy=lambda: None,
        _uncertain=[],
        _failure=None,
    )
    table = _rs.RecipeTable(2)
    ast = object()
    st = storage(0x100000000)
    miss = table.launch(dev, ast, {0: st}, (0,))
    assert isinstance(miss, _rs.RecipeMiss)
    closed = []
    program = _rs.LaunchProgram([], [], 0, (epoch, epoch.generation))
    table.insert(miss, SimpleNamespace(program=program, close=lambda: closed.append(1)))
    st.addr += 0x1000
    st.forms[FLAT].addr = st.addr
    assert table.launch(dev, ast, {0: st}, (0,)) == []
    st.forms[("pad", 16, 32)] = Form(("pad", 16, 32), st.addr + 512, 512, True)
    st.valid = {("pad", 16, 32)}
    assert isinstance(table.launch(dev, ast, {0: st}, (0,)), _rs.RecipeMiss)
    for other in (object(), object()):
        miss = table.launch(dev, other, {0: st}, (0,))
        table.insert(
            miss, SimpleNamespace(program=program, close=lambda: closed.append(1))
        )
    assert closed == [1]
    epoch.invalidate()
    with pytest.raises(ValueError, match="invalidated"):
        table.launch(dev, other, {0: st}, (0,))
    table.clear()
    assert closed == [1, 1, 1]


def test_mutant_plan_recipe_ignores_allocation_alias_is_caught():
    assert "app.recipe/no_overlap_key" in rs.mutants()
    with (
        rs.mutant("app.recipe/no_overlap_key"),
        pytest.raises(AssertionError, match="alias key"),
    ):
        alias_witness(signature)
