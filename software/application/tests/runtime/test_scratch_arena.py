"""`Device.run` binds one block per scratch arena the Rust stack laid out
(`Compiled.scratch`) under the arena's name and each member at its offset,
in the window of its space (DRAM's, a node's staging store's) that the runs in
flight share, freed once none is; an arena the caller binds stays where it is
bound."""

from types import SimpleNamespace

from kohakutpu.application.runtime.device import Device, arena_name
from kohakutpu.application.runtime.packages import Prebuilt
from kohakutpu.compiler import _rs
from kohakutpu.language import machine as MF

MACHINE = (MF.MACHINES / "multimesh_v9t6.json").read_text(encoding="utf-8")
PACKAGE = _rs.package_build('{"credit": 32}', "[]", MACHINE, defaults=False)


def arenas(dram, stage):
    return [
        {
            "space": "dram",
            "node": None,
            "bytes": dram,
            "members": [("h0", 0), ("h1", 2048)],
        },
        {"space": "stage", "node": 0, "bytes": stage, "members": [("h2", 0)]},
    ]


def test_one_block_per_arena_in_its_window(monkeypatch):
    dev = Device(kind="offline", board="multimesh_v9t6")
    input_at = dev.alloc(32)
    made, freed, seen = [], [], []
    keep_alloc, keep_free = dev.alloc, dev.free

    def alloc(nbytes, space="dram", node=None, **kwargs):
        at = keep_alloc(nbytes, space, node, **kwargs)
        made.append(at)
        return at

    def free(at):
        freed.append(at)
        keep_free(at)

    def programs(binds):
        seen.append(dict(binds))
        return {None: Prebuilt(PACKAGE, (), MACHINE, 0, (), False, None)}

    monkeypatch.setattr(dev, "alloc", alloc)
    monkeypatch.setattr(dev, "free", free)
    monkeypatch.setattr(dev.card, "run", lambda progs, **kw: {"cycles": 1})

    def submit(dram, stage, binds=()):
        compiled = SimpleNamespace(
            name="k",
            l1=SimpleNamespace(
                body=lambda name, level: SimpleNamespace(params=[]),
                params=lambda name, level: [],
            ),
            scratch=arenas(dram, stage),
            programs=programs,
        )
        return dev.submit(compiled, {"x": input_at, **dict(binds)})

    used = dev.memory.staging[0].used, dev.dram.used
    submit(4096, 512)
    dram, dram_size = dev.windows["dram", None]
    stage, stage_size = dev.windows["stage", 0]
    assert (dram_size, stage_size) == (4096, 512)
    assert seen[0] == {
        "x": input_at,
        "scratch:dram": dram,
        "h0": dram,
        "h1": dram + 2048,
        "scratch:stage:0": stage,
        "h2": stage,
    }
    submit(4096, 300)
    assert (seen[1]["scratch:dram"], seen[1]["scratch:stage:0"]) == (dram, stage)
    submit(4096, 1000)
    grown, size = dev.windows["stage", 0]
    assert seen[2]["scratch:stage:0"] == grown != stage and size == 1024
    assert made == [] and freed == [stage]
    dev.drain()
    # No run in flight: no window is kept (API.MEM.007).
    assert dev.windows == {} and sorted(freed) == sorted([stage, dram, grown])
    assert (dev.memory.staging[0].used, dev.dram.used) == used
    bound = keep_alloc(4096)
    submit(4096, 512, {"scratch:dram": bound})
    assert seen[3]["scratch:dram"] == bound and seen[3]["h1"] == bound + 2048
    assert ("dram", None) not in dev.windows
    dev.drain()
    assert arena_name(arenas(4096, 512)[1]) == "scratch:stage:0"
    keep_free(bound)
    keep_free(input_at)
