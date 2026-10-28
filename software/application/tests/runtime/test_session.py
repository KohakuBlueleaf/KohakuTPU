"""The debug session (DBG.SESS.*) over a card double: the firmware's wait
bound from the session's budget, a failure recorded and the session going on,
a FATAL firmware ending it with its console, and the budget refusing a run."""

import time

import pytest

from kohakuaccel.driver.node.boot.args import BootArgs
from kohakuaccel.driver.node.queue import FirmwareFatal, NodeError
from kohakuaccel.driver.node.queue.client import Completion
from kohakutpu.application.runtime import card as C
from kohakutpu.application.runtime.session import (
    DebugSession,
    SessionBudget,
    SessionFatal,
)


class Loader:
    def console(self) -> str:
        return "[ka] fatal trap\n"


class Card:
    """What a session touches on a `NodeCard`: boot blocks, the sysnode
    clock, the host wait, image loads and runs (scripted)."""

    drained = staticmethod(C.NodeCard.drained)

    def __init__(self, script) -> None:
        self.boots = {0: (Loader(), BootArgs(queue=0, timeout=2_000_000))}
        self.sys_hz = 100e6
        self.clock = time.monotonic
        self.timeout = 300.0
        self.debug = False
        self.loads: list = []
        self.waits: list = []
        self.script = list(script)

    def load(self, fw=None, debug: bool = False) -> None:
        self.debug = debug
        self.loads.append((debug, self.boots[0][1].timeout))

    def debug_report(self) -> dict:
        return {"packages": [{"tag": 1, "fail": {"drained": True}}]}

    def run(self, program, **kw):
        self.waits.append(self.timeout)
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def failed() -> NodeError:
    return NodeError(Completion(1, 0x30, 2, 0, 1000, 0), "")


def test_the_firmware_waits_inside_the_session_and_the_image_comes_back():
    card = Card([{"cycles": 5}])
    with DebugSession(card, seconds=8.0) as s:
        s.run("p")
    # A 2 s firmware wait at 100 MHz; the host waits for what is left (RUL.033,
    # DBG.SESS.003); the production image and its bound back after.
    assert card.loads == [(True, 200_000_000), (False, 2_000_000)]
    assert 2.0 < card.waits[0] <= 8.0
    assert card.timeout == 300.0


def test_a_failure_is_recorded_and_the_session_goes_on():
    card = Card([{"cycles": 1}, failed(), {"cycles": 2}])
    with DebugSession(card) as s:
        for _ in range(3):
            s.run("p")
    assert [r["ok"] for r in s.records] == [True, False, True]
    bad = s.records[1]
    assert bad["completion"].status == 0x30 and bad["drained"] is True
    assert bad["debug"] == {"packages": [{"tag": 1, "fail": {"drained": True}}]}


def test_a_fatal_firmware_ends_the_session_with_its_console():
    state = {"state": "fatal", "abi": 0, "done": 3, "fatal": 0xE000_0000_0000_0005}
    card = Card([FirmwareFatal(state, "[ka] fatal trap cause=5\n"), {"cycles": 1}])
    with (
        pytest.raises(SessionFatal, match="DBG.SESS.004") as e,
        DebugSession(card) as s,
    ):
        s.run("p")
        s.run("p")
    assert e.value.state["fatal"] == 0xE000_0000_0000_0005
    assert e.value.consoles == {0: "[ka] fatal trap\n"}
    # Left as it stopped: no production load after the fatal stop.
    assert [d for d, _ in card.loads] == [True]
    assert len(card.script) == 1


def test_a_run_the_budget_cannot_hold_is_refused():
    now = [0.0]
    card = Card([{"cycles": 1}, {"cycles": 2}])
    with DebugSession(card, seconds=10.0, wait=2.0, clock=lambda: now[0]) as s:
        s.run("p")
        now[0] = 8.5
        with pytest.raises(SessionBudget, match="DBG.SESS.005"):
            s.run("p")
    assert card.waits == [10.0]
