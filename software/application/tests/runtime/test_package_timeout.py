"""Scoped cleanup cannot release a package a timed-out queue still owns."""

import pytest

from kohakuaccel.driver.node.queue import layout as QL
from kohakuaccel.driver.node.queue.double import SpecFirmware
from kohakuaccel.driver.transport.memory import MemoryTransport
from kohakutpu.application.runtime.card import node_queue
from kohakutpu.application.runtime.memory import CardMemory
from kohakutpu.compiler import _rs
from kohakutpu.language import machine as MF

LAYOUT = QL.Layout.from_machine(MF.MACHINES / "multimesh_v9t6.json")


def witness(restart=False, shared=False, early_checks=True):
    host = MemoryTransport()
    memory = CardMemory((0x100000, 0x200000), {})
    now = [0.0]

    def idle():
        now[0] += 1

    q = node_queue(host, 0x80000000, memory, idle=idle, layout=LAYOUT)
    q.clock = lambda: now[0]
    q.timeout = 1
    q.init()
    fw = SpecFirmware(host, q.base, layout=LAYOUT)
    fw.boot()
    text = (MF.MACHINES / "multimesh_v9t6.json").read_text(encoding="utf-8")
    raw = _rs.package_build('{"credit": 32}', "[]", text, defaults=False)
    guard = memory.alloc(256)
    mark = memory.mark()
    tag = q.submit(raw)
    other = q.submit(raw) if shared else None
    at = next(iter(q._spilled.values()))
    with pytest.raises(TimeoutError):
        q.wait(tag)
    memory.release(mark)
    # Before any late CQ entry, the allocation is live and cannot be reused.
    if early_checks:
        assert memory.dram.owners.get(at) == "package"
        contender = memory.alloc(256)
        assert contender != at
        memory.free(contender)
    if restart:
        q.init()
        fw.boot()
    else:
        fw.step()
        q.pending()
        if shared:
            assert at in memory.dram.live
            fw.step()
            q.pending()
        assert q.wait(tag).ok
        if other is not None:
            assert q.wait(other).ok
        q.poll()
    assert at not in memory.dram.live
    assert guard in memory.dram.live
    assert not q._spilled and not q._package_tags and not q._cache
    memory.free(guard)
    q.drop_spilled()
    assert memory.dram.used == 0


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("shared", [False, True])
def test_timeout_then_late_completion_or_reset(restart, shared):
    witness(restart, shared)


def test_scope_frees_package_mutant_is_killed(monkeypatch):
    def scope_frees_package(self, mark):
        for arena, kept in zip(self.arenas(), mark, strict=True):
            for at in list(arena.live):
                if at not in kept and at not in arena.released:
                    arena.release(at)

    monkeypatch.setattr(CardMemory, "release", scope_frees_package)
    with pytest.raises(AssertionError):
        witness()
    with pytest.raises(KeyError, match="was never allocated"):
        witness(early_checks=False)
