"""RUL.011: board selection precedes opening any device or model."""

import pytest

from kohakutpu.application.runtime import card as C
from kohakutpu.application.runtime import device as D


@pytest.mark.parametrize("kind", D.KINDS)
@pytest.mark.parametrize("board", [None, ""])
def test_device_requires_a_board(monkeypatch, kind, board):
    monkeypatch.delenv(D.BOARD_ENV, raising=False)
    with pytest.raises(ValueError, match="RUL.011"):
        D.Device(kind=kind, board=board)


def test_verilator_requires_a_board_before_opening(monkeypatch):
    monkeypatch.delenv(D.BOARD_ENV, raising=False)
    with pytest.raises(ValueError, match="RUL.011"):
        C.VerilatorCard("no-model")


def test_offline_board_from_environment_and_argument(monkeypatch):
    monkeypatch.setenv(D.BOARD_ENV, "multimesh_v9t6")
    assert D.Device(kind="offline").machine.board == "multimesh_v9t6"
    assert (
        D.Device(kind="offline", board="multimesh_v9t8").machine.board
        == "multimesh_v9t8"
    )


@pytest.mark.parametrize("kind", D.KINDS)
def test_device_resolves_the_board_before_opening(monkeypatch, kind):
    monkeypatch.delenv(D.BOARD_ENV, raising=False)
    with pytest.raises(FileNotFoundError, match="no machine file"):
        D.Device(kind=kind, board="no_such_board")


def test_verilator_resolves_the_board_before_opening():
    with pytest.raises(FileNotFoundError, match="no machine file"):
        C.VerilatorCard("no-model", board="no_such_board")
