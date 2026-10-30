"""Memory lifetimes and the host-only firmware allocation/binding boundary."""

import json
import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from harness.registry import mutant, patched, runner
from tinygrad import Tensor, TinyJit
from tinygrad.dtype import dtypes

from kohakuaccel.driver.node.boot.args import REGION_QUEUE, BootArgs
from kohakuaccel.driver.node.queue import BootRefused, NodeError, NodeQueue, OverBudget
from kohakuaccel.driver.node.queue import layout as QL
from kohakuaccel.driver.node.queue.double import SpecFirmware
from kohakuaccel.driver.transport.memory import MemoryTransport
from kohakutpu.application.ktpugrad.device import KTPUGraph
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import generated
from kohakutpu.application.runtime.arena import Arena, OutOfMemory
from kohakutpu.application.runtime.device import Device
from kohakutpu.application.runtime.packages import Prebuilt
from kohakutpu.application.runtime.placement import Placement
from kohakutpu.language import machine as MF
from kohakutpu.stack import rs
from runners.host import FWB

ROOT = Path(__file__).resolve().parents[4]
IMAGE = None


@runner("memory.allocation_extents")
def allocation_extents(inp, ctx):
    dev = Device(kind="offline", board=ctx["vector"]["machine"])
    at = dev.alloc(33)
    entry, physical = dev.mmu.resolve(at)
    result = {
        "requested": dev.dram.requested[physical],
        "extent": entry.extent,
        "reserved": dev.dram.live[physical],
    }
    dev.free(at)
    dev.close()
    return result


@mutant("memory/add-allocation-tail")
def add_allocation_tail():
    original = Device.alloc
    with patched(
        Device,
        "alloc",
        lambda self, nbytes, *a, **kw: original(self, nbytes + 16384, *a, **kw),
    ):
        yield


@runner("memory.jit_drop")
def jit_drop(inp, ctx):
    dev = Device(kind="offline", board=ctx["vector"]["machine"])
    dev.record()
    at = dev.alloc(32)
    dev.write(at, bytes(32))
    log = dev.recorded()
    dev.free(at)
    graph = KTPUGraph.__new__(KTPUGraph)
    graph.log = graph.prev = ((), log, (), ())
    held = dev.dram.used > 0
    graph._release_logs()
    return {"held": held, "released": dev.dram.used == 0, "closed": log.closed}


@mutant("memory/no-jit-drop")
def no_jit_drop():
    with patched(KTPUGraph, "_release_logs", lambda self: None):
        yield


@runner("memory.jit_card")
def jit_card(inp, ctx):
    graphs = []
    original = KTPUGraph.__call__

    def observe(self, *args, **kw):
        if self not in graphs:
            graphs.append(self)
        return original(self, *args, **kw)

    @TinyJit
    def step(x):
        doubled = (x * 2).contiguous().realize()
        return (doubled + 1).contiguous().realize()

    correct = True
    dev = ctx["session"].dev
    churn = [dev.alloc(8192) for _ in range(9)]
    try:
        with D.attached(dev), patched(KTPUGraph, "__call__", observe):
            for i in range(9):
                dev.free(churn.pop())
                raw = (np.arange(144, dtype=np.float16) / 16 + i).astype(np.float16)
                x = Tensor(raw, device="KTPU", dtype=dtypes.half).realize()
                correct = bool(np.array_equal(step(x).numpy(), raw * 2 + 1)) and correct
        replayed = sum(g.counts["replayed"] for g in graphs) > 0
        print(
            "TinyJit",
            {"values": correct, "replayed": replayed},
            [(g.counts, g.misses) for g in graphs],
        )
        return {"values": correct, "replayed": replayed}
    finally:
        for at in churn:
            dev.free(at)
        for graph in graphs:
            graph._release_logs()


@mutant("memory/no-stable-placement")
def no_stable_placement():
    with patched(
        Placement,
        "alloc",
        lambda self, site, cursors, nbytes, space, node, owner: self.dev._alloc(
            nbytes, space, node, owner
        ),
    ):
        yield


@mutant("memory/no-jit-replay")
def no_jit_replay():
    with patched(Device, "replay", lambda *args, **kwargs: []):
        yield


@runner("memory.tail_sentinel")
def tail_sentinel(inp, ctx):
    dev = ctx["session"].dev
    n = 144
    reverse = inp["case"] == "reverse"
    values = np.arange(n, dtype=np.float16 if reverse else np.float32)
    name = "transpose.reverse_kernel" if reverse else "convert.cast_kernel"
    args = (
        {"src_bytes": n * 2, "levels": [[9, 32]], "at": 0, "n": n}
        if reverse
        else {"n": n, "src": "f32", "dst": "f16"}
    )
    src, dst = dev.alloc(values.nbytes + 4096), dev.alloc(n * 2 + 4096)
    sentinel = bytes([0xA5]) * 4096
    try:
        dev.write(src, values.tobytes() + bytes(4096))
        dev.write(dst, bytes(n * 2) + sentinel)
        binds = {"x": src, "y": dst} if reverse else {"in0": src, "out": dst}
        generated.launch(dev, name, args, binds)
        want = (
            values.reshape(-1, 16)[:, ::-1].flatten()
            if reverse
            else values.astype(np.float16)
        )
        return {
            "values": dev.read(dst, n * 2) == want.tobytes(),
            "sentinel": dev.read(dst + n * 2, 4096) == sentinel,
        }
    finally:
        dev.free(src)
        dev.free(dst)


#: Allocation pairs `adjacent_allocations` tries before giving up on adjacency.
ADJACENT_TRIES = 64


@runner("memory.adjacent_allocations")
def adjacent_allocations(inp, ctx):
    dev = ctx["session"].dev
    n = 144
    reverse = inp["case"] == "reverse"
    values = np.arange(n, dtype=np.float16 if reverse else np.float32)
    src = dev.alloc(8192)
    # A long session's free list need not place two fresh allocations back to
    # back; held non-adjacent pairs push the allocator to a pair that is.
    held = []
    for _ in range(ADJACENT_TRIES):
        dst, guard = dev.alloc(288), dev.alloc(4096)
        dst_entry, dst_physical = dev.mmu.resolve(dst)
        _, guard_physical = dev.mmu.resolve(guard)
        if guard_physical == dst_physical + dst_entry.arena.live[dst_physical]:
            break
        held += [dst, guard]
    for h in held:
        dev.free(h)
    sentinel = bytes([0xA5]) * 4096
    try:
        assert guard_physical == dst_physical + dst_entry.arena.live[dst_physical]
        dev.write(src, values.tobytes() + bytes(8192 - values.nbytes))
        dev.write(dst, bytes(288))
        dev.write(guard, sentinel)
        if reverse:
            generated.launch(
                dev,
                "transpose.reverse_kernel",
                {"src_bytes": 288, "levels": [[9, 32]], "at": 0, "n": n},
                {"x": src, "y": dst},
            )
            want = values.reshape(-1, 16)[:, ::-1].flatten()
        else:
            generated.launch(
                dev,
                "convert.cast_kernel",
                {"n": n, "src": "f32", "dst": "f16"},
                {"in0": src, "out": dst},
            )
            want = values.astype(np.float16)
        return {
            "values": dev.read(dst, 288) == want.tobytes(),
            "sentinel": dev.read(guard, 4096) == sentinel,
            "distinct": dst != guard
            and dst_entry.arena.requested[dst_physical] == 288
            and dst_entry.extent == -(-288 // dev.line_bytes) * dev.line_bytes,
        }
    finally:
        dev.free(src)
        dev.free(dst)
        dev.free(guard)


@runner("memory.packages")
def packages(inp, ctx):
    mem, freed = MemoryTransport(), []
    text = (MF.MACHINES / "multimesh_v9t6.json").read_text(encoding="utf-8")
    layout = QL.Layout.from_machine(text)
    q = NodeQueue(
        mem, 0x100000, spill=lambda n: 0x800000, unspill=freed.append, layout=layout
    )
    q.init()
    fw = SpecFirmware(mem, q.base, layout=layout)
    fw.boot()
    raw = rs._rs.package_build('{"credit": 32}', "[]", text, defaults=False)
    q.submit(raw)
    q.submit(raw)
    fw.step()
    q.poll()
    held = not freed
    fw.step()
    q.poll()
    return {"shared_held": held, "freed": freed, "cache_empty": not q._cache}


@mutant("memory/no-package-retirement")
def no_package_retirement():
    with patched(NodeQueue, "_retire_package", lambda *args: None):
        yield


@runner("memory.teardown")
def teardown(inp, ctx):
    dev = Device(kind="offline", board=ctx["vector"]["machine"])
    dev.record()
    at = dev.alloc(32)
    dev.write(at, bytes(32))
    log = dev.recorded()
    dev.free(at)
    dev.close()
    used = dev.dram.used
    log.close()
    return {"used": used, "closed": dev.closed}


@mutant("memory/no-teardown")
def no_teardown():
    with patched(Arena, "close", lambda *args: None):
        yield


@runner("memory.boot_regions")
def boot_regions(inp, ctx):
    dev = Device(kind="offline", board=ctx["vector"]["machine"])
    memory = dev.memory
    base = memory.stage(4096, 0, owner="firmware:queue:0")
    args = BootArgs(queue=base, timeout=1000, regions=[(base, 4096, REGION_QUEUE)])
    args.pack()
    following = memory.stage(32, 0)
    refused = False
    try:
        BootArgs(
            queue=base,
            timeout=1000,
            regions=[(base, 4096, REGION_QUEUE), (base + 32, 64, 1)],
        ).pack()
    except ValueError:
        refused = True
    return {
        "reserved": following >= base + 4096,
        "owner": memory.staging[0].owners[base],
        "overlap_refused": refused,
        "table": args.region(REGION_QUEUE) == (base, 4096),
    }


@mutant("memory/no-region-validation")
def no_region_validation():
    with patched(BootArgs, "pack", lambda self: b""):
        yield


@runner("memory.boot_region_card")
def boot_region_card(inp, ctx):
    card = ctx["session"].dev.card
    if IMAGE:
        card.load(ROOT / "build/fw" / IMAGE)
    loader, args = card.boots[card.nodes[0]]
    original = args.regions
    try:
        loader.win.stop()
        card.q.init()
        args.regions = [(card.q.base, 64, REGION_QUEUE)]
        loader.restart(card.fw, args)
        try:
            card._wait_ready(card.nodes[0], timeout=card.timeout)
        except BootRefused as e:
            return {"undersized_refused": e.reason == "queue"}
        except OverBudget:
            raise
        except (TimeoutError, RuntimeError):
            return {"undersized_refused": False}
        return {"undersized_refused": False}
    finally:
        args.regions = original
        card.load()


@runner("memory.tail_extents")
def tail_extents(inp, ctx):
    text = (MF.MACHINES / (ctx["vector"]["machine"] + ".json")).read_text(
        encoding="utf-8"
    )
    graph = json.loads(
        rs._rs.generate(
            inp["generator"], json.dumps(inp["arguments"]), text, rs._MUTANT.get()
        )
    )
    ports = graph["ports"]
    return [
        math.prod(p["spec"]["shape"]) * (4 if p["spec"]["dtype"] == "f32" else 2)
        for p in ports
    ]


@mutant("memory/allocation-tail")
def allocation_tail():
    with patched(D, "TAIL", 16384):
        yield


@mutant("memory/flat-overrun")
def flat_overrun():
    with rs.mutant("memory/flat-overrun"):
        yield


@mutant("memory/reverse-overrun")
def reverse_overrun():
    with rs.mutant("memory/reverse-overrun"):
        yield


@runner("memory.handles")
def handles(inp, ctx):
    dev = Device(kind="offline", board=ctx["vector"]["machine"])
    old = dev.alloc(32)
    physical = dev.mmu.resolve(old)[1]
    dev.free(old)
    new = dev.alloc(32)
    text = (MF.MACHINES / "multimesh_v9t6.json").read_text(encoding="utf-8")
    pkg = rs._rs.package_build(
        '{"credit": 32}',
        json.dumps(
            [["spans_from", [[0, 32]]], ["payload", 8, {"addresses": [[[0, 40, 0]]]}]]
        ),
        text,
        defaults=False,
    )
    bound = rs.bind(pkg, [new], text, 0, False, translations=dev.mmu.translations())
    got = rs._rs.package_read(bound, text)
    stale = False
    try:
        dev.mmu.resolve(old)
    except ValueError:
        stale = True
    return {
        "translated": got["payloads"] == [physical + 8],
        "fresh": new != old,
        "stale_refused": stale,
        "unbound_payload": rs._rs.package_read(pkg, text)["payloads"],
    }


@mutant("memory/identity-translation")
def identity_translation():
    with rs.mutant("l0.mmu/identity"):
        yield


@runner("memory.lifetime")
def lifetime(inp, ctx):
    dev = Device(kind="offline", board=ctx["vector"]["machine"])
    dev.max_inflight = None
    collected = []
    next_tag = iter(range(1, 10))
    dev.card = SimpleNamespace(
        submit=lambda p: next(next_tag),
        collect=lambda p: collected.append(p) or {"cycles": 0},
        drop_spilled=lambda: None,
    )
    text = (MF.MACHINES / "multimesh_v9t6.json").read_text(encoding="utf-8")
    raw = rs._rs.package_build('{"credit": 32}', "[]", text, defaults=False)
    kernel = SimpleNamespace(
        name="lifetime",
        scratch=[],
        l1=SimpleNamespace(
            body=lambda *args: SimpleNamespace(params=[]), params=lambda *args: []
        ),
        programs=lambda binds: {None: Prebuilt(raw, (), text, 0, (), False, None)},
    )
    at = dev.alloc(32)
    a = dev.submit(kernel, {"x": at, "alias": at + 16})
    b = dev.submit(kernel, {"x": at})
    dev.free(at)
    held = [dev.mmu.entries[at].live]
    dev.collect(a)
    held.append(dev.mmu.entries[at].live)
    dev.collect(b)
    held.append(dev.mmu.entries[at].live)
    return held


@runner("memory.pressure")
def pressure(inp, ctx):
    dev = object.__new__(Device)
    dev.inflight = [1, 2]
    done = []
    dev.card = SimpleNamespace(drop_spilled=lambda: None)
    dev.collect = lambda p: done.append(dev.inflight.pop(0))

    def alloc():
        if not done:
            raise OutOfMemory()
        return 123

    return {"address": dev._take(alloc), "collected": done}


@runner("memory.counters")
def counters(inp, ctx):
    a = Arena(0, 1024, 32)
    a.alloc(33, owner="input")
    gap = a.alloc(64, owner="scratch")
    a.alloc(64, owner="output")
    a.release(gap)
    return a.stats()


@runner("memory.recording")
def recording(inp, ctx):
    dev = Device(kind="offline", board=ctx["vector"]["machine"])
    initial = dev.dram.used
    dev.record()
    at = dev.alloc(32)
    dev.write(at, bytes(32))
    dev.free(at)
    log = dev.recorded()
    held = dev.mmu.entries[at].live
    log.close()
    return {"initial": initial, "held": held, "released": not dev.mmu.entries[at].live}


@mutant("memory/no-record-pins")
def no_record_pins():
    with patched(Device, "_pin_record", lambda *args: None):
        yield


@runner("memory.firmware_contract")
def firmware_contract(inp, ctx):
    layout = QL.Layout.from_machine(MF.MACHINES / "multimesh_v9t6.json")
    return {
        "opcodes": sorted(
            f"OP_{k.upper()}" for k, v in layout.opcodes.items() if v < 6
        ),
        "heap_sources": any(
            "os/heap" in p.as_posix() for p in FWB.sources(FWB.IMAGES["kohakutpu_node"])
        ),
    }


@runner("memory.firmware_card")
def firmware_card(inp, ctx):
    card = ctx["session"].dev.card
    if IMAGE:
        card.load(ROOT / "build/fw" / IMAGE)
    try:
        if inp["case"] == "relocation":
            text = (MF.MACHINES / "multimesh_v9t6.json").read_text(encoding="utf-8")
            pkg = rs._rs.package_build(
                '{"credit": 32}',
                json.dumps(
                    [
                        ["spans_from", [[0x400000, 32]]],
                        ["payload", 0x400000, {"addresses": [[[0, 40, 0]]]}],
                    ]
                ),
                text,
                defaults=False,
            )
            try:
                return card.q.run(pkg).status
            except NodeError as e:
                return e.completion.status
        result = []
        for op in (3, 4, 5):
            tag = card.q.submit(op=op, words=[0, 0x400000, 64, 32])
            try:
                result.append(card.q.wait(tag).status)
            except NodeError as e:
                result.append(e.completion.status)
        return result
    finally:
        if IMAGE:
            card.load()


@mutant("memory/free-immediate")
def free_immediate():
    def release(self, at):
        self.released.add(at)
        self._release(at)

    with patched(Arena, "release", release):
        yield


@mutant("memory/drain-all")
def drain_all():
    def take(self, alloc):
        try:
            return alloc()
        except OutOfMemory:
            for p in list(self.inflight):
                self.collect(p)
            return alloc()

    with patched(Device, "_take", take):
        yield


@mutant("memory/no-peak")
def no_peak():
    original = Arena.stats

    def stats(self):
        out = original(self)
        out["peak"] = out["used"]
        return out

    with patched(Arena, "stats", stats):
        yield


@mutant("memory/heap-image")
def heap_image():
    with patched(FWB, "DISPATCH_EXCLUDE", ()):
        yield


@mutant("memory/firmware-refusals-disabled")
def firmware_refusals_disabled():
    global IMAGE
    keep, IMAGE = IMAGE, "kohakutpu_memory_mutant.elf"
    try:
        yield
    finally:
        IMAGE = keep
