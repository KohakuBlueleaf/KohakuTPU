"""A probe session owns empty signal slots; its later runs retain leftovers."""

import json
import struct
import sys
from typing import Any

import hrun
import pytest

from h import gen
from h import ledger as L
from h.gen.body import HEAD, Body
from h.tests.test_runner import STAT_SENT, Card
from kohakutpu.application.runtime.card import interlink
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.driver.host import board_map


@pytest.mark.parametrize("control_bytes", (4, 8))
def test_session_initialises_slots_once_without_hiding_later_residue(
    monkeypatch, tmp_path, control_bytes
):
    slots = {n: {1: 1, 2: 1} for n in range(4)}
    sweeps = []
    booted = False
    closed = []
    issued = 0

    def act(card, prog):
        nonlocal issued
        slots[0][1] += 1
        slots[0][1] -= 1
        card.write(card.bound(1), struct.pack("<Q", (1 << L.SLOT_SHIFT) | slots[0][1]))
        card.bump(0, interlink.STATW + 8 * 4, 1)  # A ring from sysnode 2.
        card.bump(2, STAT_SENT, 1)
        if issued == 0:
            slots[0][1] += 1
        issued += 1
        return dict(cycles=5, nodes={0: dict(cycles=5), 2: dict(cycles=5)})

    card: Any = Card(act)
    card.nodes = (0, 2)
    card.machine = machine_of("multimesh_v9t6", nodes=3)
    card.boot_flags = {0: 0, 2: 0}
    card.fw = tmp_path / "firmware.elf"
    card.close = lambda: closed.append(True)
    ctrls = board_map(card.board)["ctrl"]
    enabled = {0: True, 1: True, 2: False, 3: True}
    for n, control in enumerate(ctrls):
        card.t.words[control + interlink.STATW] = int(enabled[n])
        card.t.words[control + interlink.STATW + 8] = 4

    def write(at, value):
        assert booted
        n = ctrls.index(at - interlink.CONTROL)
        card.t.words[ctrls[n] + interlink.STATW] = value & 1
        if value & 8:
            assert value & 6 == 0  # Neither doorbells nor faults are cleared.
            slots[n] = dict.fromkeys(slots[n], 0)
            sweeps.append(n)
        if value & 4:
            card.t.words[ctrls[n] + interlink.STATW + 8] = 0

    def wrong_width(*args):
        pytest.fail("a control write must use the transport's register width")

    card.t.control_bytes = control_bytes
    card.t.write32 = write if control_bytes == 4 else wrong_width
    card.t.write64 = write if control_bytes == 8 else wrong_width

    def boot(*args, **kwargs):
        nonlocal booted
        booted = True
        return card

    def timebase(got):
        assert got is card
        assert sweeps == [0, 2]
        assert slots[1] == slots[3] == {1: 1, 2: 1}
        for n, control in enumerate(ctrls):
            assert card.t.words[control + interlink.STATW] == enabled[n]
            if issued == 0:
                assert card.t.words[control + interlink.STATW + 8] == 4
        return {}

    text, specs = HEAD, {}
    for name in ("first", "second"):
        body = Body(ledger=True)
        body.ring(2, 0, 1)
        body.take(0, 1, 1)
        text += body.fn(name, "unused: u64[1] @dram")
        specs[name] = gen.Spec(("slots",), ledger=body.ledger_of())
    monkeypatch.setattr(hrun.gen, "load", lambda *args: gen.Module(text, specs))
    monkeypatch.setattr(hrun, "open_transport", lambda name: card.t)
    monkeypatch.setattr(hrun, "clocks_at", lambda *args: {})
    monkeypatch.setattr(hrun, "SiliconCard", boot)
    monkeypatch.setattr(hrun.meta, "image", lambda path: {})
    monkeypatch.setattr(hrun.meta, "commit", dict)
    monkeypatch.setattr(hrun.meta, "timebase", timebase)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hrun",
            "--board",
            "multimesh_v9t6",
            "--profile",
            "mid",
            "--nodes",
            "0,2",
            "--gen",
            "q0",
            "--reps",
            "2",
            "--faults",
            "--out",
            str(tmp_path),
        ],
    )

    assert hrun.main() == 0
    rows = [
        json.loads(line) for line in (tmp_path / "runs.jsonl").read_text().splitlines()
    ]
    assert [row["verdict"]["structural"] for row in rows] == [
        "PASS",
        "FAIL",
        "FAIL",
        "FAIL",
    ]
    assert all(row["ledger"] == rows[1]["ledger"] for row in rows[1:])
    assert closed == [True]
