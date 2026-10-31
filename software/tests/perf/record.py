"""Record a workload's launches on the offline device: every kernel's L2 and L1, and
in order what each launch ran on. The later perf tools read these traces and never
tinygrad.

    python software/tests/perf/record.py lenet --board multimesh_v9t6
    python software/tests/perf/record.py vit --library build/templates --blocks 12
"""

import argparse
import json
import pathlib
import runpy
import signal
import sys
import time

from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.card import NodeCard
from kohakutpu.stack import rs

ROOT = pathlib.Path(__file__).resolve().parents[3]
TRACES = ROOT / "build" / "perf" / "traces"
EXAMPLES = ROOT / "software" / "kohakutpu" / "application" / "examples" / "tinygrad"
WORKLOADS = {
    "mlp": ("mnist_mlp.py", ["--model", "mlp", "--batch", "8", "--steps", "3"]),
    "lenet": ("mnist_mlp.py", ["--model", "lenet", "--batch", "8", "--steps", "3"]),
    "vit": ("vit_tiny.py", ["--batch", "1", "--inputs", "3"]),
}


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


def recording(trace: Trace, library: pathlib.Path | None):
    """`Device` offline, each submit recorded, each library miss prebuilt."""
    submit = D.Device.submit
    compile_graph = D.Device.compile_graph

    def on_submit(self, compiled, binds, kind="dsl", info=None):
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
    D.Device = Offline


def on_card(out: pathlib.Path):
    """Each run's card record in run order; the call given back writes them."""
    records, devices = [], []
    result = NodeCard._result
    init = D.Device.__init__

    def on_result(self, *args, **kw):
        got = result(self, *args, **kw)
        records.append(dict(got.get("phases") or {}, cycles=got["cycles"]))
        return got

    def on_init(self, *args, **kw):
        init(self, *args, **kw)
        devices.append(self)

    def write():
        rows = [
            dict(name=r.name, kind=r.kind, wall=r.wall, **p)
            for r, p in zip(devices[-1].runs, records, strict=False)
        ]
        text = "\n".join(json.dumps(r) for r in rows)
        (out / "card.jsonl").write_text(text + "\n", encoding="utf-8")

    NodeCard._result = on_result
    D.Device.__init__ = on_init
    return write


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("workload", choices=WORKLOADS)
    ap.add_argument("--card", action="store_true")
    ap.add_argument("--jit", action="store_true", help="TinyJit: replays skip submit")
    ap.add_argument("--board", default="multimesh_v9t6")
    ap.add_argument("--nodes", type=int, default=1)
    ap.add_argument("--library", type=pathlib.Path)
    ap.add_argument("--blocks", type=int)
    ap.add_argument("--steps", type=int)
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    script, extra = WORKLOADS[args.workload]
    extra = list(extra) + ["--board", args.board, "--nodes", str(args.nodes)]
    if args.library:
        extra += ["--library", str(args.library), "--library-only"]
    if script == "mnist_mlp.py":
        extra += ["--test", "8"] if args.jit else ["--no-jit", "--test", "8"]
    if args.steps:
        extra += ["--steps", str(args.steps)]
    if args.blocks:
        extra += ["--blocks", str(args.blocks)]
    if args.card:
        extra += ["--device", "card", "--transport", "xdma"]

    name = f"{args.workload}{args.tag}.{args.board}.{args.nodes}n"
    trace = Trace(TRACES / name)
    write = on_card(trace.out) if args.card else None
    if not args.card:
        recording(trace, args.library)
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
    except BaseException as error:  # noqa: BLE001
        done = repr(error)[:2000]
    if write is not None:
        write()
    trace.save(
        dict(
            workload=args.workload,
            board=args.board,
            nodes=args.nodes,
            args=extra,
            seconds=round(time.monotonic() - t0, 2),
            done=done,
        )
    )
    print(name, trace.count, "launches", len(trace.kernels), "kernels", done)
    return 0 if done == "exited" else 1


if __name__ == "__main__":
    sys.exit(main())
