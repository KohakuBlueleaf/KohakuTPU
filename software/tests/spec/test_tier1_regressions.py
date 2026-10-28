"""Host regressions for tier-1 contracts, without opening a target."""

from types import SimpleNamespace

import pytest
from runners import memory

from kohakuaccel.driver.node.boot.args import ERR_QUEUE, EXIT_BOOTARGS, REGION_QUEUE
from kohakuaccel.driver.node.queue import NodeQueue
from kohakuaccel.driver.node.queue import layout as QL
from kohakuaccel.driver.transport.memory import MemoryTransport
from kohakutpu.application.runtime.device import Device


@pytest.mark.parametrize("board", ["multimesh_v9t6", "vlt_v9t6s_1n"])
def test_adjacent_requested_extent_and_physical_reservation(board):
    dev = Device(kind="offline", board=board)
    src, dst, guard = dev.alloc(8192), dev.alloc(288), dev.alloc(4096)
    entry, physical = dev.mmu.resolve(dst)
    other, guard_physical = dev.mmu.resolve(guard)
    assert dev.line_bytes == 64
    assert dev.dram.requested[physical] == 288
    assert entry.extent == 320
    assert dev.dram.live[physical] == 512
    assert guard_physical == physical + 512
    assert entry is not other and dst != guard
    # The existing vector's distinct field also requires a 288-byte extent.
    assert (entry.extent == 288 and dst != guard) is False
    for at in (src, dst, guard):
        dev.free(at)
    dev.close()


def boot_probe(monkeypatch, short=False):
    ticks = [0.0]
    transport = MemoryTransport()
    q = NodeQueue(transport, 0x100000, clock=lambda: ticks[0])
    args = SimpleNamespace(regions=[(q.base, q.size, REGION_QUEUE)])
    original = list(args.regions)
    stopped, restored = [], []

    def idle():
        ticks[0] += 0.005
        if ticks[0] >= 0.025:
            transport.write_block(
                q.base + QL.FW,
                QL.line(QL.ST_FATAL, 0, 0, EXIT_BOOTARGS | ERR_QUEUE),
            )

    q._idle = idle
    loader = SimpleNamespace(
        win=SimpleNamespace(stop=lambda: stopped.append(True)),
        restart=lambda *a: None,
    )
    card = SimpleNamespace(
        nodes=(0,),
        q=q,
        boots={0: (loader, args)},
        fw=None,
        timeout=1.0,
        _wait_ready=lambda n, timeout: q.wait_ready(0.01 if short else timeout),
        load=lambda: restored.append(list(args.regions)),
    )
    monkeypatch.setattr(memory, "IMAGE", None)
    got = memory.boot_region_card(
        {}, {"session": SimpleNamespace(dev=SimpleNamespace(card=card))}
    )
    assert stopped and restored == [original]
    assert got == {"undersized_refused": True}


def test_boot_probe_waits_for_a_refusal_after_ten_milliseconds(monkeypatch):
    boot_probe(monkeypatch)


def test_ten_millisecond_boot_probe_mutant_is_killed(monkeypatch):
    with pytest.raises(AssertionError):
        boot_probe(monkeypatch, short=True)
