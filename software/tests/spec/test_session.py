"""The tier-1 sim session opens every node of its Verilated build
(`runners.card.Session`, the meshes whose agent answers); a
session forced to one node (``KTPU_SPEC_NODES=1``) cannot run a program
on another. Runs with ``KTPU_SPEC_CARD=sim`` and ``KOHAKUTPU_SIM_BUILD``.
"""

import os
import struct

import pytest
from harness.registry import MUTANTS, patched
from runners import card as RC
from runners.target import l1 as RL

from kohakuaccel.driver.node.boot import args as BA
from kohakuaccel.driver.node.queue import BootRefused

#: A body whose second node writes a buffer the first does not touch.
TWO_NODES = {
    "text": (
        "target ktpu.v9\n"
        "fn f.l1(a: u64[4] @dram, b: u64[4] @dram)\n"
        "    node 0\n"
        "        store a <- 1\n"
        "    node 1\n"
        "        store b <- 2\n"
    ),
    "fn": "f",
    "read": ["a", "b"],
}

sim = pytest.mark.skipif(
    os.environ.get("KTPU_SPEC_CARD") != "sim", reason="tier 1: KTPU_SPEC_CARD=sim"
)


@sim
def test_the_session_opens_every_node_of_the_build():
    n = RC.build_nodes(RC.sim_build(), RC.sim_board())
    s = RC.Session("sim")
    try:
        assert s.dev.card.nodes == tuple(range(n))
        if n > 1:
            got = RL.card(TWO_NODES, {"session": s})
            assert (got["a"][0], got["b"][0]) == (1, 2)
    finally:
        s.close()


@sim
def test_a_session_forced_to_one_node_runs_no_second_node(monkeypatch):
    if RC.build_nodes(RC.sim_build(), RC.sim_board()) < 2:
        pytest.skip("a one-node build")
    monkeypatch.setenv(RC.NODES_ENV, "1")
    s = RC.Session("sim")
    try:
        assert s.dev.card.nodes == (0,)
        try:
            got = RL.card(TWO_NODES, {"session": s})
        except Exception as e:
            assert "node" in str(e).lower() or "mesh" in str(e).lower(), e
            return
        assert got["b"][0] != 2, "node 1's store ran on a one-node session"
    finally:
        s.close()


@sim
def test_a_refused_boot_reports_its_reason_at_once():
    s = RC.Session("sim")
    card = s.dev.card
    _, args = card.boots[0]
    base = bytearray(args.pack())
    cases = [
        (0, 0x32544F4F42414B, BA.ERR_MAGIC, "no magic"),
        (1, 99, BA.ERR_VERSION, "version"),
        (8, BA.MAX_UNITS + 1, BA.ERR_UNITS, "units"),
        (9 + BA.MAX_UNITS + 1, 0, BA.ERR_REGIONS, "regions"),
    ]
    try:
        for word, val, code, reason in cases:
            raw = bytearray(base)
            struct.pack_into("<Q", raw, 8 * word, val)
            with patched(args, "pack", lambda r=bytes(raw): r):
                with pytest.raises(BootRefused, match=reason) as e:
                    card.restart(ready=0.01, nodes=(0,))
                assert e.value.code == code and e.value.reason == reason
                # Witness: when the refusal is ignored, the ready wait times out.
                with MUTANTS["l0.boot/ignore_refusal"]():
                    loader, _ = card.boots[0]
                    loader.win.stop()
                    card.queues[0].init()
                    loader.restart(card.fw, args)
                    with pytest.raises(TimeoutError, match="'state': 'fatal'"):
                        card.queues[0].wait_ready(timeout=0.0001)
        card.restart(nodes=(0,))
    finally:
        s.close()
