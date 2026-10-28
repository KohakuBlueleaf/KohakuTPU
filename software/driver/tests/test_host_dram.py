"""The host's windows and scratch reservation follow the machine capacity."""

import pytest

from kohakutpu.driver import host
from kohakutpu.driver.clock.card import load_board
from kohakutpu.language import machine


@pytest.mark.parametrize("name", [n for n in machine.names() if "-" not in n])
def test_host_map_takes_capacity_from_the_machine(name):
    board = load_board(name)
    mp = host.board_map(board)
    assert mp["size"] == machine.load(name).get("card.dram.bytes")
    assert mp["scratch"] == mp["arena_limit"] == mp["size"] - host.SCRATCH_RESERVE
    board["mem_words"] = 1
    assert host.board_map(board) == mp


@pytest.mark.parametrize("size", [64 << 20, 512 << 20, 8 << 30])
def test_map_has_no_capacity_default(monkeypatch, size):
    m = machine.load("multimesh_v9t6").dict()
    m["card"]["dram"]["bytes"] = size
    monkeypatch.setattr(host, "load_machine", lambda _: machine.Machine(m))
    mp = host.board_map(load_board("multimesh_v9t6"))
    assert mp["size"] == size
    assert mp["arena_limit"] == size - host.SCRATCH_RESERVE


def test_an_unnamed_card_is_refused_before_opening_a_transport():
    with pytest.raises(ValueError, match="named board"):
        host.Card()


def test_mesh_preflight_uses_its_own_scratch():
    writes = []
    mesh = object.__new__(host.Mesh)
    mesh.mem_size = 64 << 20
    mesh.mem = type(
        "Memory",
        (),
        {
            "write_block": lambda _, a, b: writes.append((a, b)),
            "read_block": lambda _, a, n: writes[-1][1],
        },
    )()
    mesh.verify_write_path()
    assert writes[0][0] == mesh.mem_size - host.SCRATCH_RESERVE
