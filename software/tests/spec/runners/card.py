"""The tier-1 card session: one device for every tier-1 vector, node 0 at the
idle profile over XDMA on the Linux host (``KTPU_SPEC_CARD=<board>``), or a
Verilated card (``KTPU_SPEC_CARD=sim``).

``KTPU_SPEC_TIMEOUT=<seconds>`` bounds every package on the card (the host's
wait and the firmware's) so a hang fails its test with the node's report; on
the Verilated card, whose waits count modelled time, it is the wall-clock
alarm (`OverBudget`: the test is skipped with the model's cycles);
``KTPU_SPEC_STEPS=1`` adds the firmware's per-step trace to that report.

A Verilated session opens every node of its build (``KOHAKUTPU_SIM_BUILD``,
on ``KOHAKUTPU_BOARD``): the meshes, from 0, whose agent answers
(`build_nodes`). ``KTPU_SPEC_NODES=<n>`` opens the first n instead."""

import os
import pathlib
import tempfile

from kohakuaccel.driver.node.boot.args import BootArgs
from kohakuaccel.driver.transport.simdram import SimDram
from kohakuaccel.driver.transport.verilator import VerilatorTransport
from kohakutpu.application.runtime import card as C
from kohakutpu.application.runtime.device import (
    BOARD_ENV,
    BUILD_ENV,
    Device,
    default_build,
)
from kohakutpu.driver.clock.card import load_board
from kohakutpu.driver.host import Card, board_map
from kohakutpu.language import machine

#: Boot flag: a PKGS line per step (`ka/boot/args.h` KA_BOOT_F_STEPS).
STEPS = 8
#: Unit kinds and their machine-file sections.
KINDS = {"MG": "mg", "VC": "vc", "SR": "sr"}
#: A Verilated session's node count, for a test that needs a subset.
NODES_ENV = "KTPU_SPEC_NODES"


def sim_build() -> pathlib.Path:
    """The Verilated build a sim session opens (`Device`'s choice)."""
    return pathlib.Path(os.environ.get(BUILD_ENV) or default_build(1))


def sim_board() -> str:
    """The board a sim session opens (`VerilatorCard`'s default without one)."""
    return os.environ.get(BOARD_ENV) or "multimesh_v9"


def build_nodes(build, board: str) -> int:
    """The nodes Verilated build `build` has on `board`: its meshes, from 0,
    whose agent answers enumeration (a build's other dies hold no agent)."""
    b = load_board(board)
    mp = board_map(b)
    log = pathlib.Path(tempfile.mkdtemp(prefix="ktpu_nodes_")) / "model.log"
    t = VerilatorTransport(build, log=log)
    try:
        host = SimDram(t, b, mp["mem"], mp["size"])
        n = 0
        while n < b["meshes"]:
            try:
                Card.from_board(
                    board, transport=host, which=[n], verify=False, reply_at_agent=True
                )
            except RuntimeError:
                break
            n += 1
    finally:
        t.close()
    return n


class Session:
    """One open device; tier-1 runners take it from their context."""

    def __init__(self, board: str):
        seconds = float(os.environ.get("KTPU_SPEC_TIMEOUT", "0"))
        if board == "sim":
            build = sim_build()
            nodes = int(os.environ.get(NODES_ENV) or build_nodes(build, sim_board()))
            self.dev = Device(kind="sim", build=build, nodes=nodes)
            if seconds:
                self.dev.card.alarm = seconds
            for _, args in self.dev.card.boots.values():
                args.flags |= C.F_TIMING
            self.dev.card.restart()
            return
        self.dev = Device(kind="card", board=board, transport="xdma", nodes=1)
        steps = os.environ.get("KTPU_SPEC_STEPS") == "1"
        card = self.dev.card
        sys_hz = card.board["profiles"][card.board["idle_profile"]]["sys"] * 1e6
        for _, args in card.boots.values():
            assert isinstance(args, BootArgs)
            args.flags |= C.F_TIMING
            if seconds:
                args.timeout = int(seconds * sys_hz)
            if steps:
                args.flags |= STEPS
        if seconds:
            card.timeout = 2 * seconds
        card.restart()

    @property
    def machine(self):
        """The machine file of the board the target opened (the
        card checked its units against what it enumerates), whatever machine
        a vector names."""
        return machine.load(self.dev.card.machine.board)

    def units(self) -> dict:
        """``{kind: [[x, y], ...]}`` of the units the target enumerates on its
        first node (the machine-file sections: mg, vc, sr)."""
        got = self.dev.card.machine.mesh(self.dev.card.nodes[0]).units
        return {
            sec: sorted(list(c) for c in got.get(k, ())) for k, sec in KINDS.items()
        }

    def differs(self, name: str) -> str | None:
        """Why a vector written for machine file `name` (its die's units)
        does not describe this target, or None when the units are the same."""
        units = machine.load(name).die.units.dict()
        want = {
            sec: sorted(list(c) for c in units.get(sec, ())) for sec in KINDS.values()
        }
        got = self.units()
        if want == got:
            return None
        n = ", ".join(f"{len(v)} {k.upper()}" for k, v in want.items() if v)
        t = ", ".join(f"{len(v)} {k.upper()}" for k, v in got.items() if v)
        board = self.dev.card.board["name"]
        return f"the vector's die ({name}: {n}) is not the target's ({board}: {t})"

    def ready(self) -> None:
        """The firmware running and its queue empty before a vector: a node a
        mutant left down (a boot block it rejected), or holding a package an
        earlier test's wait gave up on (its next package would queue behind
        it and time out), is restarted with the session's own."""
        card = self.dev.card
        if card.debug:
            card.load(debug=False)
            return
        queues = card.queues.values()
        if any(q.state()["state"] != "ready" or q.pending() for q in queues):
            card.restart()

    def close(self) -> None:
        self.dev.close()


__all__ = ["Session"]
