"""Horizontal fusion refuses RAW, WAR, WAW and representation aliases."""

from types import SimpleNamespace

import pytest

from kohakutpu.application.ktpugrad.launch_schedule import groups


def storage(addr, size=256, forms=None):
    return SimpleNamespace(addr=addr, nbytes=size, forms=forms or {})


def plan(output=0, reads=(1,)):
    return SimpleNamespace(
        out_slot=output,
        accesses=[SimpleNamespace(slot=i) for i in reads],
        out_shape=(128,),
        out_dtype="f16",
    )


@pytest.mark.parametrize("hazard", ["raw", "war", "waw", "form", "partial"])
def test_refuses_alias_and_mutant(hazard):
    a, b, c, d = [storage(i * 1024) for i in range(4)]
    first, second = {0: a, 1: b}, {0: c, 1: d}
    if hazard == "raw":
        second[1] = a
    elif hazard == "war":
        second[0] = b
    elif hazard == "waw":
        second[0] = a
    elif hazard == "form":
        second[1] = storage(
            8192, forms={"view": SimpleNamespace(addr=a.addr, nbytes=256)}
        )
    else:
        second[1] = storage(a.addr + 32)
    items = [(plan(), first), (plan(), second)]
    assert groups(items, lambda p: True) == [([0], set()), ([1], set())]
    assert groups(items, lambda p: True, ignore_hazards=True) == [([0, 1], set())]


def test_independent_members_keep_schedule_order():
    a, b, c = [storage(i * 1024) for i in range(3)]
    items = [(plan(), {0: a, 1: c}), (plan(), {0: b, 1: c})]
    assert groups(items, lambda p: True) == [([0, 1], set())]
