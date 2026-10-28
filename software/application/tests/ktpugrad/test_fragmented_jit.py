"""TinyJit placement is independent of the device's earlier heap history."""

import gc
import weakref

import numpy as np
import pytest
from tinygrad import Tensor, TinyJit

from kohakutpu.application.ktpugrad.device import KTPUGraph
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.placement import Placement


def fragment(dev):
    """Keep separators between differently sized first-fit holes."""
    rng = np.random.default_rng(11)
    blocks = [dev.alloc(int(n) * 256) for n in rng.integers(1, 17, 64)]
    holes = set(rng.choice(len(blocks), 42, replace=False))
    for i in holes:
        dev.free(blocks[i])
    return [b for i, b in enumerate(blocks) if i not in holes]


def exercise(dev, monkeypatch):
    graphs, placements = [], []
    original_call, original_alloc = KTPUGraph.__call__, D.Device.alloc

    def observe(self, *args, **kwargs):
        if self not in graphs:
            graphs.append(self)
        return original_call(self, *args, **kwargs)

    def allocate(self, *args, **kwargs):
        at = original_alloc(self, *args, **kwargs)
        if self is dev and self.recording:
            entry, physical = self.mmu.resolve(at)
            placements.append((entry.arena.owners[physical], args[0], physical))
        return at

    monkeypatch.setattr(KTPUGraph, "__call__", observe)
    monkeypatch.setattr(D.Device, "alloc", allocate)

    @TinyJit
    def step(x):
        doubled = (x * 2).contiguous().realize()
        return (doubled + 1).contiguous().realize()

    churn = [dev.alloc(8192) for _ in range(12)]
    separators = fragment(dev)
    with D.attached(dev):
        for i in range(12):
            dev.free(churn[-1 - i])
            raw = np.arange(144, dtype=np.float16) / 16 + i
            x = Tensor(raw, device="KTPU").realize()
            step(x)
            # Perturb the general heap between graph calls as well.
            noise = [dev.alloc(n) for n in (768, 256, 1024)]
            for at in reversed(noise):
                dev.free(at)
        dev.drain()
    print("placements", placements)
    print("graphs", [(g.counts, g.misses) for g in graphs])
    for at in separators:
        dev.free(at)
    return step, graphs


@pytest.mark.parametrize("seed", [0, 7])
def test_fragmented_heap_replays(monkeypatch, seed):
    dev = D.Device(kind="offline", board="vlt_v9t6s_1n")
    prefix = [dev.alloc(256 * (seed + 1)) for _ in range(seed)]
    try:
        step, graphs = exercise(dev, monkeypatch)
        assert graphs and sum(g.counts["replayed"] for g in graphs) >= 3
        assert all(g.log is not None and g.log[1].references for g in graphs)
        pools = [g.placement for g in graphs]
        refs = {ref for pool in pools for ref in pool.references}
        weak = [weakref.ref(g) for g in graphs]
        del step
        graphs.clear()
        gc.collect()
        assert all(g() is None for g in weak)
        assert all(p.closed and not p.references for p in pools)
        assert all(at not in arena.live for arena, at in refs)
        for at in prefix:
            dev.free(at)
    finally:
        dev.close()


def test_no_stable_placement_mutant_is_killed(monkeypatch):
    monkeypatch.setattr(
        Placement,
        "alloc",
        lambda self, site, cursors, nbytes, space, node, owner: self.dev._alloc(
            nbytes, space, node, owner
        ),
    )
    with pytest.raises(AssertionError):
        test_fragmented_heap_replays(monkeypatch, 0)
