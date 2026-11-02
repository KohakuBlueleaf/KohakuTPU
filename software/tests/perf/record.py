"""Record a workload's launches on the offline device or the card: every kernel's L2
and L1, in order what each launch ran on, the host's time around each. The later perf
tools read these traces and never tinygrad.

    python software/tests/perf/record.py lenet --board multimesh_v9t6
    python software/tests/perf/record.py vit --library build/templates --blocks 12
    python software/tests/perf/record.py unet --card --measure --profile 0:40
"""

import argparse
import collections
import cProfile
import json
import os
import pathlib
import pstats
import runpy
import signal
import struct
import sys
import time
import traceback

import zoo
from examples.tinygrad import mnist_models

from kohakuaccel.driver.node.queue import layout as QL
from kohakutpu.application.ktpugrad.device import KTPUAllocator, KTPUGraph
from kohakutpu.application.runtime import card as C
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import generated, packages, rows
from kohakutpu.application.runtime.card import NodeCard
from kohakutpu.stack import rs

ROOT = pathlib.Path(__file__).resolve().parents[3]
TRACES = ROOT / "build" / "perf" / "traces"
#: ViT's CPU reference logits, one file a configuration (`vit_tiny.py --references`).
REFERENCES = ROOT / "build" / "perf" / "references"
EXAMPLES = ROOT / "software" / "kohakutpu" / "application" / "examples" / "tinygrad"
MEASURE_FIRMWARE = ROOT / "build" / "fw" / "kohakutpu_measure.elf"
MNIST = (
    "mlp",
    "lenet",
    "lenet-bn",
    "lenet-nonorm",
    "resnet-s",
    "convnet-w",
    "mixer",
    "gpt",
    "gpt4",
    "unet",
    "gru",
    "wide",
)
WORKLOADS = {
    **{
        m: ("mnist_mlp.py", ["--model", m, "--batch", "8", "--steps", "3"])
        for m in MNIST
    },
    "vit": ("vit_tiny.py", ["--batch", "1", "--inputs", "10"]),
    "vitb": ("vit_tiny.py", ["--batch", "1", "--inputs", "10", "--width", "768"]),
    "nodes": ("nodes_measure.py", ["--batch", "1", "--inputs", "8"]),
}
#: Where the measure image finds its ring, and a ring slot: run, start, cycles, MVSTAT,
#: units, then per unit its coordinate, CU_COUNTERS and CU_DBG (interp.c `measure`).
RING_OFF = 0xFC000
RING_BYTES = 256
RING_SLOTS = 1 << 15


class Ended(BaseException):
    """The run's bound came."""


def ended(*_):
    raise Ended


class Trace:
    """One workload's kernels on disk, its launches in order."""

    def __init__(self, out: pathlib.Path):
        self.out = out
        (out / "kernels").mkdir(parents=True, exist_ok=True)
        self.kernels: dict[str, str] = {}
        self.launches = (out / "launches.jsonl").open("w", encoding="utf-8")
        self.count = 0
        self.compiles: list[tuple] = []
        self.ended: float | None = None
        self.row: dict = {}

    def kernel(self, compiled) -> None:
        if compiled.name in self.kernels:
            return
        self.kernels[compiled.name] = compiled.name
        (self.out / "kernels" / f"{compiled.name}.l2").write_text(
            compiled.l2_text, encoding="utf-8"
        )
        (self.out / "kernels" / f"{compiled.name}.l1").write_text(
            compiled.l1_text, encoding="utf-8"
        )

    def compiled(self, name: str, seconds: float) -> None:
        """A library fill spans bounded rounds, so its compile times append."""
        self.compiles.append((name, seconds))
        with (self.out / "compiles.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(dict(name=name, seconds=seconds)) + "\n")

    def submitted(self, seconds: float) -> None:
        """A launch's host cost goes out with it: the gap before, the submit."""
        self.row["submit_ms"] = round(seconds * 1e3, 3)
        self.launches.write(json.dumps(self.row) + "\n")
        self.count += 1

    def launch(self, dev, compiled, binds: dict, kind: str, info, gap: float) -> None:
        self.kernel(compiled)
        phys = {p: int(dev.mmu.resolve(a)[1]) for p, a in binds.items()}
        keep = {
            k: v
            for k, v in dict(info or {}).items()
            if isinstance(v, (str, int, float, bool))
        }
        scratch = getattr(compiled, "scratch", None) or []
        self.row = dict(
            name=compiled.name,
            kind=kind,
            binds=phys,
            scratch=sum(a["bytes"] for a in scratch),
            info=keep,
            gap_ms=round(gap * 1e3, 3),
        )

    def save(self, meta: dict) -> None:
        self.launches.close()
        meta = dict(
            meta,
            launches=self.count,
            kernels=len(self.kernels),
            compiles=self.compiles,
        )
        (self.out / "meta.json").write_text(json.dumps(meta, indent=1), "utf-8")


class Spans:
    """Inclusive host time of the layers a launch crosses."""

    def __init__(self):
        self.totals = collections.defaultdict(lambda: [0, 0.0])

    def wrap(self, owner, attr: str, name: str) -> None:
        fn = getattr(owner, attr)

        def call(*args, **kw):
            t0 = time.perf_counter()
            try:
                return fn(*args, **kw)
            finally:
                entry = self.totals[name]
                entry[0] += 1
                entry[1] += time.perf_counter() - t0

        setattr(owner, attr, call)


class Profile:
    """cProfile over launches [first, last) of the run."""

    def __init__(self, span: str | None):
        first, last = (int(v) for v in (span or "-1:-1").split(":"))
        self.first, self.last = first, last
        self.profile = cProfile.Profile()
        self.on = False
        self.reached = False

    def at(self, launch: int) -> None:
        if launch == self.first and not self.on:
            self.profile.enable()
            self.on = self.reached = True
        elif launch == self.last and self.on:
            self.profile.disable()
            self.on = False

    def write(self, out: pathlib.Path) -> None:
        if not self.reached:
            return
        if self.on:
            self.profile.disable()
        stats = pstats.Stats(self.profile).stats  # type: ignore[attr-defined]
        rows_ = [
            dict(file=f, line=ln, fn=fn, calls=nc, own=tt, cumulative=ct)
            for (f, ln, fn), (_, nc, tt, ct, _) in stats.items()
            if tt > 1e-5
        ]
        rows_.sort(key=lambda r: -r["own"])
        (out / "profile.json").write_text(json.dumps(rows_, indent=0), "utf-8")


def recording(trace: Trace, library: pathlib.Path | None, card: bool, prof: Profile):
    """`Device` with each submit recorded, each library miss prebuilt, offline unless
    on the card."""
    submit = D.Device.submit
    compile_graph = D.Device.compile_graph

    def on_submit(self, compiled, binds, kind="dsl", info=None):
        prof.at(trace.count)
        t0 = time.perf_counter()
        gap, trace.ended = t0 - (trace.ended or t0), None
        trace.launch(self, compiled, binds, kind, info, gap)
        got = submit(self, compiled, binds, kind, info)
        done = time.perf_counter()
        trace.ended = done
        trace.submitted(done - t0)
        return got

    def on_compile(self, source):
        if library is None:
            return compile_graph(self, source)
        misses = self.counters["library_misses"]
        try:
            return compile_graph(self, source)
        except RuntimeError:
            self.counters["library_misses"] = misses
            t0 = time.monotonic()
            rs.library_prebuild(library, source, rs.machine_json(self.machine))
            name = json.loads(source)["name"]
            trace.compiled(name, round(time.monotonic() - t0, 3))
            print("compiled", len(trace.compiles), trace.compiles[-1], flush=True)
            return compile_graph(self, source)

    class Offline(D.Device):
        def __init__(self, kind="offline", *args, **kw):
            kw.pop("transport", None)
            super().__init__("offline", *args, **kw)

    D.Device.submit = on_submit
    D.Device.compile_graph = on_compile
    if not card:
        D.Device = Offline


def host_spans() -> Spans:
    spans = Spans()
    spans.wrap(generated, "launch", "generate_launch")
    spans.wrap(rows, "run", "rows")
    spans.wrap(D.Device, "submit", "submit")
    spans.wrap(D.Device, "_collect_one", "collect")
    spans.wrap(D.Device, "compile_graph", "compile_graph")
    spans.wrap(packages, "built", "bind_admit")
    # A TinyJit call: its input uploads, state walk, replay publish and restore.
    spans.wrap(KTPUGraph, "__call__", "graph_call")
    spans.wrap(KTPUGraph, "_fill", "graph_fill")
    spans.wrap(KTPUGraph, "_storages", "graph_storages")
    spans.wrap(KTPUGraph, "_restore", "graph_restore")
    spans.wrap(KTPUAllocator, "_copyin", "copyin")
    spans.wrap(KTPUAllocator, "_copyout", "copyout")
    spans.wrap(D.Device, "replay", "replay")
    return spans


def ring_open(device, ring: dict) -> None:
    """Give each node's measure image a ring in DRAM."""
    card = device.card
    for n, q in card.queues.items():
        at = card.memory.alloc(RING_SLOTS * RING_BYTES, owner=f"measure:{n}")
        q.size = RING_OFF
        card.mem.write64(q.base + RING_OFF, at)
        card.mem.write64(q.base + RING_OFF + 8, RING_SLOTS)
        card.mem.write64(q.base + RING_OFF + 16, 0)
        ring[n] = at


def ring_rows(device, ring: dict) -> list[dict]:
    """The ring's records, one a run, in node order."""
    card = device.card
    rows_ = []
    for n, at in ring.items():
        count = card.mem.read64(card.queues[n].base + RING_OFF + 16)
        kept = min(count, RING_SLOTS)
        for i in range(kept):
            raw = card.mem.read_block(at + i * RING_BYTES, RING_BYTES)
            w = struct.unpack("<32Q", raw)
            units = []
            for u in range(min(w[4], 8)):
                coord, counters, dbg = w[5 + 3 * u : 8 + 3 * u]
                units.append(
                    dict(
                        x=coord & 0xFF,
                        y=(coord >> 8) & 0xFF,
                        type=coord >> 32,
                        retired=counters >> 32,
                        busy=counters & 0xFFFFFFFF,
                        dbg_hi=dbg >> 32,
                        dbg_lo=dbg & 0xFFFFFFFF,
                    )
                )
            rows_.append(
                dict(
                    node=n,
                    run=w[0],
                    start=w[1],
                    cycles=w[2],
                    moves=w[3] & 0xFFFFFFF,
                    units=units,
                )
            )
    return rows_


def card_rows(runs, records: list[dict], ring: list[int] | None) -> list[dict]:
    """One row a run, from `runs` (each with name, kind, wall) and the card's
    `records`, one a collection. An eager list (`list_index` n > 1, its runs not a
    TinyJit replay) is one record for its n runs: each run takes its own ring
    `cycles` as `cycles` and `steps`, the first also the list's other phases and
    `list_cycles`; without a ring, the run's own cycles. A replay is one row."""
    rows_, at, slot = [], 0, 0
    for rec in records:
        n = rec.get("list_index", 0)
        if at < len(runs) and runs[at].kind != "replay" and n > 1:
            for k, run in enumerate(runs[at : at + n]):
                cycles = ring[slot + k] if ring is not None else run.cycles
                phases = dict(rec) if k == 0 else {key: 0 for key in rec}
                phases |= dict(cycles=cycles, steps=cycles, list_index=0)
                phases |= dict(in_list=n, list_pos=k, item_tag=rec.get("item_tag", 0))
                if k == 0:
                    phases["list_cycles"] = rec["cycles"]
                rows_.append(
                    dict(name=run.name, kind=run.kind, wall=run.wall, **phases)
                )
            at, slot = at + n, slot + n
            continue
        if at < len(runs):
            run = runs[at]
            rows_.append(dict(name=run.name, kind=run.kind, wall=run.wall, **rec))
            slot += max(1, n) if run.kind == "replay" else 1
        at += 1
    return rows_


def on_card(out: pathlib.Path, measure: bool, traced: bool):
    """Each run's card record in run order; the call given back writes them. `traced`
    boots the debug image, whose step records come back with each result."""
    records, devices, ring = [], [], {}
    result = NodeCard._result
    collect = NodeCard.collect
    card_submit = NodeCard.submit
    card_init = NodeCard.__init__
    init = D.Device.__init__

    def on_result(self, *args, **kw):
        got = result(self, *args, **kw)
        records.append(dict(got.get("phases") or {}, cycles=got["cycles"]))
        return got

    def on_collect(self, p, *args, **kw):
        got = collect(self, p, *args, **kw)
        if "debug" in got:
            records[-1]["trace"] = got["debug"]
        return got

    def on_init(self, *args, **kw):
        init(self, *args, **kw)
        devices.append(self)
        if measure:
            ring_open(self, ring)

    def write():
        device = devices[-1]
        slots = ring_rows(device, ring) if measure else None
        cycles = [r["cycles"] for r in slots] if slots is not None else None
        rows_ = card_rows(device.runs, records, cycles)
        text = "\n".join(json.dumps(r) for r in rows_)
        (out / "card.jsonl").write_text(text + "\n", encoding="utf-8")
        if slots is not None:
            units = "\n".join(json.dumps(r) for r in slots)
            (out / "units.jsonl").write_text(units + "\n", encoding="utf-8")
        return dict(
            counters={k: v for k, v in device.counters.items()},
            runs=len(device.runs),
            transport={
                str(k): v
                for k, v in (
                    device.card.mem.snapshot()
                    if hasattr(device.card.mem, "snapshot")
                    else {}
                ).items()
            },
        )

    def on_card_init(self, *args, **kw):
        card_init(self, *args, **dict(kw, debug=True))

    def on_card_submit(self, program, trace=False, flags=0, after=None):
        return card_submit(self, program, True, flags | QL.RUN_F_NOENGINE, after)

    NodeCard._result = on_result
    D.Device.__init__ = on_init
    if traced:
        C.DEBUG_RECORDS = 1 << 18
        NodeCard.__init__ = on_card_init
        NodeCard.collect = on_collect
        NodeCard.submit = on_card_submit
    return write


def output_check(report: pathlib.Path) -> dict | None:
    """The run's output statistics against CPU f32 and simulated MXFP7, None when it
    wrote no report or checked nothing."""
    if not report.exists():
        return None
    return json.loads(report.read_text("utf-8")).get("check")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("workload", choices=WORKLOADS)
    ap.add_argument("--card", action="store_true")
    ap.add_argument("--measure", action="store_true", help="unit counters per launch")
    ap.add_argument("--traced", action="store_true", help="debug image, step records")
    ap.add_argument("--jit", action="store_true", help="TinyJit: replays skip submit")
    ap.add_argument("--board", default="multimesh_v9t6")
    ap.add_argument("--nodes", type=int, default=1)
    ap.add_argument("--library", type=pathlib.Path)
    ap.add_argument("--blocks", type=int)
    ap.add_argument("--steps", type=int)
    ap.add_argument("--batch", type=int)
    ap.add_argument("--profile", help="cProfile launches FIRST:LAST")
    ap.add_argument("--tag", default="")
    ap.add_argument("--extra", nargs="*", default=[], help="the workload's own flags")
    args = ap.parse_args()

    script, extra = WORKLOADS[args.workload]
    extra = list(extra) + ["--board", args.board, "--nodes", str(args.nodes)]
    if args.batch:
        extra[extra.index("--batch") + 1] = str(args.batch)
    if args.library:
        extra += ["--library", str(args.library), "--library-only"]
    if not args.jit:
        extra += ["--no-jit"]
    if script == "mnist_mlp.py":
        extra += ["--test", "8"]
    if args.steps:
        steps = "--steps" if script == "mnist_mlp.py" else "--inputs"
        extra += [steps, str(args.steps)]
    if args.blocks:
        extra += ["--blocks", str(args.blocks)]
    if args.card:
        extra += ["--device", "card", "--transport", "xdma"]
    extra += args.extra

    name = f"{args.workload}{args.tag}.{args.board}.{args.nodes}n"
    if args.card and (TRACES / name / "meta.json").exists():
        print(name, "exists: one run a configuration", flush=True)
        return 1
    trace = Trace(TRACES / name)
    report = trace.out / "report.json"
    if args.card and script != "nodes_measure.py":
        extra += ["--report", str(report)]
        extra += ["--check", "8"] if script == "mnist_mlp.py" else []
        extra += ["--references", str(REFERENCES)] if script == "vit_tiny.py" else []
    write = on_card(trace.out, args.measure, args.traced) if args.card else None
    if args.measure:
        C.FIRMWARE = MEASURE_FIRMWARE
    prof = Profile(args.profile)
    recording(trace, args.library, args.card, prof)
    spans = host_spans()
    mnist_models.MODELS.update(zoo.MODELS)
    sys.argv = [str(EXAMPLES / script), *extra]
    signal.signal(signal.SIGTERM, ended)
    t0 = time.monotonic()
    done = "exited"
    try:
        runpy.run_path(sys.argv[0], run_name="__main__")
    except Ended:
        done = "stopped at its bound"
    except SystemExit as end:
        if end.code:
            done = f"exit {end.code}"
    except BaseException:  # noqa: BLE001
        done = traceback.format_exc()[-1800:]
    seconds = round(time.monotonic() - t0, 2)
    if done != "exited":
        print(done, flush=True)
    prof.write(trace.out)
    card_meta = write() if write is not None else {}
    trace.save(
        dict(
            card_meta,
            workload=args.workload,
            board=args.board,
            nodes=args.nodes,
            args=extra,
            seconds=seconds,
            done=done,
            spans={k: dict(calls=v[0], seconds=v[1]) for k, v in spans.totals.items()},
            profile=os.environ.get("KOHAKUTPU_PROFILE"),
            # Which paths ran: every stack knob this process saw, and the four dies'
            # clocks as `bringup read-clocks` read them before the run.
            knobs={
                k: v
                for k, v in sorted(os.environ.items())
                if k.startswith(("KTPU_", "KOHAKUTPU_"))
            },
            clocks=json.loads(os.environ.get("PERF_CLOCKS", "null")),
            check=output_check(report),
            profiled=[prof.first, prof.last] if prof.reached else None,
            jit=args.jit,
            commit=os.environ.get("PERF_COMMIT"),
            firmware=str(C.FIRMWARE),
            when=time.time(),
        )
    )
    print(name, trace.count, "launches", len(trace.kernels), "kernels", done)
    return 0 if done == "exited" else 1


if __name__ == "__main__":
    sys.exit(main())
