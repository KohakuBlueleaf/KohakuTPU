"""The machine a card's programs are built for comes from the units the card
enumerates (`card.enumerated`), not from a board file."""

import pytest

from kohakutpu.application.runtime import card as C
from kohakutpu.application.runtime.machine import machine_of


class Mesh:
    def __init__(self, units: dict, index: int = 0) -> None:
        self.units = units
        self.index = index
        self.endpoints = []

    def coords(self, kind: str) -> tuple:
        return tuple(self.units.get(kind, ()))


TWO = {"MG": [(1, 2), (1, 1)], "VC": [(2, 1), (2, 2)]}


def test_a_mesh_where_no_unit_answered_is_refused():
    # The v9t6 card over XDMA (2026-10-22): every CU_CAPS read unanswered,
    # a machine with no MG or VC built from it, and every kernel failing later.
    with pytest.raises(C.CardMismatch, match="no unit answered"):
        C.enumerated([Mesh({})])
    with pytest.raises(C.CardMismatch, match="mesh 1: no unit answered"):
        C.enumerated([Mesh(TWO), Mesh({}, index=1)])


def test_every_mesh_alike_gives_its_units():
    got = C.enumerated([Mesh(TWO), Mesh(TWO)])
    assert got == {"MG": [(1, 1), (1, 2)], "VC": [(2, 1), (2, 2)], "SR": []}


@pytest.mark.parametrize(
    "board,regs,copies", [("multimesh_v9t6", 128, 2), ("multimesh_v9", 32, 4)]
)
def test_the_vector_core_build_comes_from_the_machine_file(board, regs, copies):
    m = machine_of(board, units=TWO)
    assert (m.params["VC"]["regs"], m.params["VC"]["copies"]) == (regs, copies)


def test_meshes_that_differ_are_refused():
    four = {**TWO, "MG": [(1, 0), (1, 1), (1, 2), (1, 3)]}
    with pytest.raises(C.CardMismatch, match="different units"):
        C.enumerated([Mesh(TWO), Mesh(four)])
