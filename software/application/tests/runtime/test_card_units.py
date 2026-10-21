"""The machine a card's programs are built for comes from the units the card
enumerates (`card.enumerated`), not from a board file."""

import pytest

from kohakutpu.application.runtime import card as C


class Mesh:
    def __init__(self, units: dict) -> None:
        self.units = units

    def coords(self, kind: str) -> tuple:
        return tuple(self.units.get(kind, ()))


TWO = {"MG": [(1, 2), (1, 1)], "VC": [(2, 1), (2, 2)]}


def test_every_mesh_alike_gives_its_units():
    got = C.enumerated([Mesh(TWO), Mesh(TWO)])
    assert got == {"MG": [(1, 1), (1, 2)], "VC": [(2, 1), (2, 2)], "SR": []}


def test_meshes_that_differ_are_refused():
    four = {**TWO, "MG": [(1, 0), (1, 1), (1, 2), (1, 3)]}
    with pytest.raises(C.CardMismatch, match="different units"):
        C.enumerated([Mesh(TWO), Mesh(four)])
