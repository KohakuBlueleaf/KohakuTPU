"""LeNet on the 2+2 board through the OS lowering against ka-package's, the writer of
every stream the card has run: each unit's and the mover's WAITs, in order, must agree.
Then the whole step as replay.py publishes it: one fused stream, played on a double of
the engine, every WAIT reached."""

import pathlib
import pickle
import struct
import sys

import pytest

from kohakutpu.application.runtime import forms
from kohakutpu.application.runtime.device import Device, Recorded
from kohakutpu.application.runtime.os import abi as A
from kohakutpu.application.runtime.os.graph import Graph

SOFTWARE = pathlib.Path(A.__file__).parents[6]
sys.path.insert(0, str(SOFTWARE / "kohakutpu/os/tools/ostpu"))
sys.path.insert(0, str(SOFTWARE / "kohakuaccel/os/tools"))

from boot import load
from capture import capture_workload
from relocate import relocate

BOARD = "multimesh_v9t6"
OS_BOARD = "card_v9t6_4n_solo"
ARENA = 0x4000_0000


def table(raw):
    h = struct.unpack_from(A.PKG_HDR_FMT, raw)
    n = (h[A.KA_PH_COUNTS2] >> 32) & 0xFFFF
    coords = []
    for i in range(n):
        u = struct.unpack_from("<Q", raw, h[A.KA_PH_OFF_UNIT] + 16 * i)[0]
        coords.append(((u >> 16) & 255, u & 255, (u >> 8) & 255))
    return coords


def writer_waits(raw):
    """(src, want) of every WAIT in a lowered package's ENGINE steps."""
    h = struct.unpack_from(A.PKG_HDR_FMT, raw)
    out = []
    for i in range((h[A.KA_PH_COUNTS2] >> 16) & 0xFFFF):
        a, arg = struct.unpack_from("<QQ", raw, h[A.KA_PH_OFF_STEP] + 16 * i)
        if a & 255 != A.KA_OP_ENGINE:
            continue
        for k in range(a >> 32):
            at = h[A.KA_PH_OFF_PAY] + 32 * (arg + k // 3)
            group = struct.unpack_from("<4Q", raw, at)
            code, value = (group[0] >> (8 * (k % 3))) & 255, group[k % 3 + 1]
            if code == A.KOS_DE_WAIT_CODE:
                out.append((value >> 56, value & A.KOS_DE_CTR_MASK))
    return out


def graph_waits(raw):
    """The same of the OS lowering, sources back in package units."""
    coords = table(raw)
    units = {c: i for i, c in enumerate(coords)}
    g = Graph(compact=True, movers=True, de_stream=True, de_fetch=True)
    (task,) = g.package(raw, [], units)
    stream = g.streams[task]
    out = []
    for k in range(g.tasks[task].words[1]):
        group = struct.unpack_from("<4Q", stream, 32 * (k // 3))
        code, value = (group[0] >> (8 * (k % 3))) & 255, group[k % 3 + 1]
        if code == A.KOS_DE_WAIT_CODE:
            out.append((value >> 56, value & A.KOS_DE_CTR_MASK))
    return out


def awaits_past_a_barrier(raw):
    """Whether an AWAIT by count follows a BARRIER, what a BARRIER counted as raised
    twice over."""
    h = struct.unpack_from(A.PKG_HDR_FMT, raw)
    seen = False
    for i in range((h[A.KA_PH_COUNTS2] >> 16) & 0xFFFF):
        a = struct.unpack_from("<Q", raw, h[A.KA_PH_OFF_STEP] + 16 * i)[0]
        if a & 255 == A.KA_OP_AWAIT and seen and not (a >> 8) & 255:
            return True
        seen |= a & 255 == A.KA_OP_BARRIER
    return False


@pytest.fixture(scope="module")
def captured():
    """The composed reads a real model plans: the suite's default holds them off."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(forms, "COMPOSED", forms._PREPARED_DEFAULTS["composed"])
        return capture_workload("lenet", BOARD)


def test_every_wait_of_lenet_is_the_writers(captured):
    programs = [p for e in captured["step"] if e[0] == "program" for p in e[1].values()]
    assert any(awaits_past_a_barrier(p.bound(False)) for p in programs)
    for p in programs:
        raw = p.bound(False)
        written = writer_waits(p.bound(True))
        assert written
        assert graph_waits(raw) == written


class Session:
    """What OSCard asks of a booted node's resident session."""

    io = None
    data_pa, data_ref, data_bytes, capacity = ARENA, ARENA, 1 << 28, 1 << 20

    def prepare(self, graph):
        self.graph = graph


def play(graph, task, units):
    """The stream's WAITs against words retired at their GO and moves at theirs."""
    count, dst, arg2, arg3, moves, waits = {}, 0, 0, 0, 0, 0
    stream = graph.streams[task]
    for k in range(graph.tasks[task].words[1]):
        group = struct.unpack_from("<4Q", stream, 32 * (k // 3))
        code, value = (group[0] >> (8 * (k % 3))) & 255, group[k % 3 + 1]
        base = code & A.KOS_DE_CODE_MASK
        if base == A.KOS_DE_WAIT_CODE:
            src, want = value >> 56, value & A.KOS_DE_CTR_MASK
            have = moves if src == A.KOS_DE_MOVER_SRC else count.get(src, 0)
            behind = (have - want) & A.KOS_DE_CTR_MASK
            assert behind <= A.KOS_DE_CTR_MASK >> 1, f"WAIT({src}, {want}) at {have}"
            waits += 1
        elif base == A.KOS_DE_MB_DST:
            dst = value
        elif base == A.KOS_DE_MB_ARG2:
            arg2 = value
        elif base == A.KOS_DE_MB_ARG3:
            arg3 = value
        elif base == A.KOS_DE_MB_GO:
            fetch = dst >= A.KOS_DE_FETCH_TYPED
            at = arg2 >> 40 if fetch else (dst >> 4) & 0xF0 | dst & 15
            n = arg3 & 255 if fetch else 1
            unit = units[(0, at & 15, at >> 4)]
            count[unit] = (count.get(unit, 0) + n) & A.KOS_DE_CTR_MASK
        elif base == A.KOS_DE_MV_CTRL and value & A.KOS_MV_CTRL_GO:
            moves = (moves + 1) & A.KOS_DE_CTR_MASK
    return waits


def test_the_step_replay_publishes_reaches_every_wait(captured):
    capture = pickle.loads(pickle.dumps(captured))
    units = {
        (u["mesh"], u["x"], u["y"]): i for i, u in enumerate(load(OS_BOARD)["units"])
    }
    session = Session()
    dev = Device("os", board=BOARD, os_session=session, os_units=units)
    dev.card.segment, dev.card.compact, dev.card.movers = 8, True, True
    dev.replay_overcommit = True
    events = relocate(capture["step"], capture["base"], ARENA, capture["extent"])
    log = Recorded(events, [], dev)
    dev.prepare_recorded(log)
    graph = session.graph
    assert log.prepared.launches > 20
    assert play(graph, 0, units) > log.prepared.launches
    assert len(graph.streams) == 1
    log.close()
