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
from kohakutpu.application.runtime import card as C
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import generated, packages, rows
from kohakutpu.application.runtime.card import NodeCard
from kohakutpu.stack import rs

ROOT = pathlib.Path(__file__).resolve().parents[3]
TRACES = ROOT / "build" / "perf" / "traces"
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
    "unet",
    "gru",
    "wide",
)
WORKLOADS = {
    **{
        m: ("mnist_mlp.py", ["--model", m, "--batch", "8", "--steps", "3"])
        for m in MNIST
    },
    "vit": ("vit_tiny.py", ["--batch", "1", "--inputs", "3"]),
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
            trace.compiles.append((name, round(time.monotonic() - t0, 3)))
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
        rows_ = [
            dict(name=r.name, kind=r.kind, wall=r.wall, **p)
            for r, p in zip(device.runs, records, strict=False)
        ]
        text = "\n".join(json.dumps(r) for r in rows_)
        (out / "card.jsonl").write_text(text + "\n", encoding="utf-8")
        if measure:
            units = "\n".join(json.dumps(r) for r in ring_rows(device, ring))
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
    args = ap.parse_args()

    script, extra = WORKLOADS[args.workload]
    extra = list(extra) + ["--board", args.board, "--nodes", str(args.nodes)]
    if args.batch:
        extra[extra.index("--batch") + 1] = str(args.batch)
    if args.library:
        extra += ["--library", str(args.library), "--library-only"]
    if script == "mnist_mlp.py":
        extra += ["--test", "8"] if args.jit else ["--no-jit", "--test", "8"]
    if args.steps:
        steps = "--steps" if script == "mnist_mlp.py" else "--inputs"
        extra += [steps, str(args.steps)]
    if args.blocks:
        extra += ["--blocks", str(args.blocks)]
    if args.card:
        extra += ["--device", "card", "--transport", "xdma"]

    name = f"{args.workload}{args.tag}.{args.board}.{args.nodes}n"
    if args.card and (TRACES / name / "meta.json").exists():
        print(name, "exists: one run a configuration", flush=True)
        return 1
    trace = Trace(TRACES / name)
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
    except SystemExit:
        pass
    except BaseException:  # noqa: BLE001
        done = traceback.format_exc()[-1800:]
    seconds = round(time.monotonic() - t0, 2)
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
            commit=os.environ.get("PERF_COMMIT"),
            firmware=str(C.FIRMWARE),
            when=time.time(),
        )
    )
    print(name, trace.count, "launches", len(trace.kernels), "kernels", done)
    return 0 if done == "exited" else 1


if __name__ == "__main__":
    sys.exit(main())
