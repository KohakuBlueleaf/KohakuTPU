"""`Device.run` allocates one block per scratch arena the Rust stack laid
out (`Compiled.scratch`), binds it under the arena's name and each member at
its offset, and frees every block after the run."""

from types import SimpleNamespace

from kohakutpu.application.runtime.device import Device, arena_name
from kohakutpu.application.runtime.packages import Prebuilt
from kohakutpu.compiler import _rs
from kohakutpu.language import machine as MF

MACHINE = (MF.MACHINES / "multimesh_v9t6.json").read_text(encoding="utf-8")
PACKAGE = _rs.package_build('{"credit": 32}', "[]", MACHINE, defaults=False)

ARENAS = [
    {
        "space": "dram",
        "node": None,
        "bytes": 4096,
        "members": [("h0", 0), ("h1", 2048)],
    },
    {"space": "stage", "node": 0, "bytes": 512, "members": [("h2", 0)]},
]


def test_one_block_per_arena_bound_by_name_and_member_offset(monkeypatch):
    dev = Device(kind="offline", board="multimesh_v9t6")
    input_at = dev.alloc(32)
    made, freed, seen = [], [], {}
    keep_alloc, keep_free = dev.alloc, dev.free

    def alloc(nbytes, space="dram", node=None, **kwargs):
        at = keep_alloc(nbytes, space, node, **kwargs)
        made.append((at, nbytes, space))
        return at

    def free(at):
        freed.append(at)
        keep_free(at)

    def programs(binds):
        seen.update(binds)
        return {None: Prebuilt(PACKAGE, (), MACHINE, 0, (), False, None)}

    monkeypatch.setattr(dev, "alloc", alloc)
    monkeypatch.setattr(dev, "free", free)
    monkeypatch.setattr(dev.card, "run", lambda progs, **kw: {"cycles": 1})
    compiled = SimpleNamespace(
        name="k",
        l1=SimpleNamespace(
            body=lambda name, level: SimpleNamespace(params=[]),
            params=lambda name, level: [],
        ),
        scratch=ARENAS,
        programs=programs,
    )
    dev.run(compiled, {"x": input_at})
    assert [(n, s) for _, n, s in made] == [(4096, "dram"), (512, "stage")]
    dram, stage = made[0][0], made[1][0]
    assert seen == {
        "x": input_at,
        "scratch:dram": dram,
        "h0": dram,
        "h1": dram + 2048,
        "scratch:stage:0": stage,
        "h2": stage,
    }
    assert sorted(freed) == sorted(at for at, _, _ in made)
    assert arena_name(ARENAS[1]) == "scratch:stage:0"
    keep_free(input_at)
