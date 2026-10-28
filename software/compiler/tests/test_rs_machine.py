"""Runtime builds retain their named machine's fetch port, not a units match."""

import json

import pytest

from kohakutpu.application.runtime import packages as build
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.language import machine as MF
from kohakutpu.stack import rs

BOARD = "multimesh_v9t6"
PAIR = {"pair_a": [0, 2], "pair_b": [0, 3]}


@pytest.fixture
def pair(monkeypatch, tmp_path):
    original = json.loads(rs.machine_json(BOARD))
    for name, port in PAIR.items():
        doc = json.loads(json.dumps(original))
        doc["machine"]["die"]["fetch_port"] = port
        (tmp_path / f"{name}.json").write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr(MF, "MACHINES", tmp_path)
    monkeypatch.delenv(rs.MACHINE_ENV, raising=False)


def fetches():
    return {name: rs.fetch(machine_of(name)) for name in PAIR}


def test_the_machine_file_is_the_boards(pair):
    assert fetches() == {n: tuple(p) for n, p in PAIR.items()}


def test_picking_a_file_by_units_fails_the_check(pair, monkeypatch):
    first = machine_of("pair_a").machine_json
    monkeypatch.setattr(rs, "machine_json", lambda spec: first)
    assert fetches() != {n: tuple(p) for n, p in PAIR.items()}


def test_a_board_without_a_machine_file_is_refused():
    with pytest.raises((ValueError, FileNotFoundError)):
        machine_of("no_such_board")


def test_a_rust_package_for_another_fetch_port_is_refused():
    p = build.Prebuilt(b"", (), "", 0, (), False, (0, 2))
    with pytest.raises(ValueError, match="fetch port"):
        build.built({0: p}, None, (0, 3))
    with pytest.raises(ValueError, match="fetch port"):
        build.built({0: p}, None, None)
