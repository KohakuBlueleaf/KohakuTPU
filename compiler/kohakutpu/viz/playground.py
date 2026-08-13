"""Compile one kernel and report every level it passed through.

The source defines `kernel_fn`, a `@kernel` function, and `INPUTS`, a dict of
parameter name to shape. It may define `REFERENCE(**arrays)`, returning the
float64 answer -- the shipped examples all do, because a level that renders is
not the same as a level that is right.

NOTHING HERE RAISES. A person typing into a textarea produces a syntax error
every few keystrokes, and each one belongs in the error panel rather than in a
traceback. Returns a dict of text panels plus `error`, None on success.
"""

import io
import json
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import numpy as np

PAGE = Path(__file__).with_name("playground.html")

#: The panels the page renders, in order. `check` is last because it is the
#: only one that can say the kernel was WRONG rather than merely expensive.
PANELS = ("dsl", "stages", "instructions", "cost", "mesh", "check")


def _blank() -> dict:
    return {name: "" for name in PANELS}


def compile_kernel(source: str, seed: int = 0) -> dict:
    """Run `source` and return one text panel per level.

    Returns `{**panels, "error": str | None, "stats": dict}`. `stats` carries
    the relative and absolute error distributions against `REFERENCE`, or is
    empty when the source defines none.
    """
    out = _blank()
    out["error"] = None
    out["stats"] = {}
    try:
        return _compile(source, seed, out)
    except Exception:  # noqa: BLE001 -- every failure is a panel, not a crash
        out["error"] = traceback.format_exc(limit=6)
        return out


def _compile(source: str, seed: int, out: dict) -> dict:
    from kohakuaccel.lang import dims, loop, units
    from kohakutpu.lang import kernel

    from kohakutpu import kernels as K
    from kohakutpu import lang as L
    from kohakutpu import ops as O
    from kohakutpu import sim

    ns: dict = {
        "np": np,
        "L": L,
        "K": K,
        "O": O,
        "kernel": kernel,
        "dims": dims,
        "units": units,
        "loop": loop,
    }
    exec(compile(source, "<kernel>", "exec"), ns)  # noqa: S102
    # ONE accepted name. A second would let a source compile quietly against
    # the function its author did not mean.
    fn, shapes = ns.get("kernel_fn"), ns.get("INPUTS")
    if fn is None or shapes is None:
        raise ValueError(
            "source must define a @kernel named `kernel_fn` and a dict `INPUTS` "
            "of parameter name to shape"
        )

    rng = np.random.default_rng(seed)
    arrays = {
        name: np.asarray(rng.normal(0, 0.4, tuple(shape)), np.float16)
        for name, shape in shapes.items()
    }
    knobs = dict(ns.get("KNOBS") or {})

    out["dsl"] = source.strip()
    got = sim.observe(fn, *arrays.values(), **knobs)

    out["stages"] = _stages(got.compiled)
    out["instructions"] = _instructions(got.compiled)
    out["cost"] = got.timing.report()
    out["mesh"] = _mesh(got)
    out["check"], out["stats"] = _check(got, ns.get("REFERENCE"), arrays)
    return out


def _stages(made) -> str:
    """The compiled plan: stages, grids, and every statement of instance 0."""
    lines = [made.pretty(), ""]
    for stage in made.stages:
        stmts = stage.instances[0].stmts if stage.instances else []
        lines.append(
            f"stage {stage.index} {stage.unit} grid{stage.grid} "
            f"x{len(stage.instances)} instances"
        )
        for s in stmts:
            args = {
                k: v
                for k, v in s.args.items()
                if k in ("i", "j", "gm", "gn", "nk", "operand", "result", "part")
            }
            lines.append(f"    {s.kind:<8} {args}")
    if made.conversions:
        lines += ["", f"RELAYOUTS: {made.conversions}"]
    return "\n".join(lines)


def _instructions(made) -> str:
    """Level 1 and 0: the decoded instruction stream of one instance per stage.

    A cluster flit and a vector flit are DIFFERENT instruction sets, so each
    stage is decoded with its own; reading one with the other's field table
    produces plausible nonsense rather than an error.
    """
    from kohakutpu.isa import ISA
    from kohakutpu.isa.vector import ISA as VEC
    from kohakutpu.lang import BACKEND

    addrs = {
        n: 0x1000 * (i + 1)
        for i, n in enumerate(
            [p.name for p in made.signature.ports]
            + list(made.temps)
            + list(BACKEND.constants(made))
        )
    }
    lines = []
    for stage in made.stages:
        try:
            words = BACKEND.encode(made, stage, addrs)
        except Exception as why:  # noqa: BLE001 -- report, never raise
            lines.append(f"stage {stage.index}: could not encode ({why})")
            continue
        at = min(words) if words else None
        stream = words.get(at, []) if at is not None else []
        which = VEC if stage.unit == "VC" else ISA
        lines.append(
            f"stage {stage.index} {stage.unit} instance {at}: "
            f"{len(stream)} words, {which.set.name}"
        )
        kinds: dict = {}
        shown = 0
        for word in stream:
            try:
                name, fields = which.set.decode(word)
            except Exception:  # noqa: BLE001 -- report, never raise
                name, fields = "?", {}
            kinds[name] = kinds.get(name, 0) + 1
            # An IMEM word is one ALU instruction of a vector program; listing
            # 200 of them buries the shape, so the mix below carries them.
            if shown < 16 and name != "IMEM":
                live = {k: v for k, v in fields.items() if v}
                lines.append(f"    {name:<7} {live}")
                shown += 1
        lines.append(f"    mix: {kinds}")
    return "\n".join(lines)


def _mesh(got) -> str:
    """What crossed the machine, and the instruction mix each unit retired."""
    lines = [f"{k:<12} {v:>12,}" for k, v in got.counters.items()]
    lines += [
        "",
        f"stages       {got.stages:>12}   <- MAG round trips",
        f"temps        {len(got.temps):>12}   {got.temps}",
        "",
        f"clusters     {got.clusters}",
        f"cores        {got.cores}",
        f"saturated    {got.saturated:>12}",
    ]
    return "\n".join(lines)


def _check(got, reference, arrays) -> tuple:
    """The error distribution against `REFERENCE`, and a text panel for it."""
    if reference is None:
        return ("no REFERENCE defined, so nothing was graded", {})
    want = np.asarray(
        reference(**{k: v.astype(np.float64) for k, v in arrays.items()}),
        np.float64,
    )
    mine = np.asarray(got.result, np.float64)
    if mine.shape != want.shape:
        return (f"SHAPE {mine.shape} against a reference of {want.shape}", {})
    diff = np.abs(mine - want)
    scale = np.maximum(np.abs(want), 1e-6)
    rel = {
        "p50": float(np.median(diff / scale)),
        "p99": float(np.percentile(diff / scale, 99)),
        "max": float((diff / scale).max()),
    }
    absolute = {
        "p50": float(np.median(diff)),
        "p99": float(np.percentile(diff, 99)),
        "max": float(diff.max()),
    }
    peak = float(diff.max() / max(np.abs(want).max(), 1e-9))
    panel = "\n".join(
        [
            f"against float64, over {mine.size:,} elements",
            "",
            (
                f"  relative   p50 {rel['p50']:.3e}   p99 {rel['p99']:.3e}"
                f"   max {rel['max']:.3e}"
            ),
            (
                f"  absolute   p50 {absolute['p50']:.3e}"
                f"   p99 {absolute['p99']:.3e}   max {absolute['max']:.3e}"
            ),
            f"  max as a fraction of the reference's peak: {peak:.3e}",
            "",
            "A contraction on this machine sits near 1e-2: L1 holds MXFP7, not",
            "fp16. An elementwise pass never goes near it and sits near 1e-4.",
        ]
    )
    return (panel, {"rel": rel, "abs": absolute, "peak": peak})


# ------------------------------------------------------------------- the page
class _Handler(BaseHTTPRequestHandler):
    """Serves the page, and answers `POST /compile` with the panels."""

    def log_message(self, fmt, *args) -> None:
        """Quiet: a playground reloads constantly and the log is noise."""

    def do_GET(self) -> None:
        body = PAGE.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        size = int(self.headers.get("Content-Length") or 0)
        source = json.loads(self.rfile.read(size) or b"{}").get("source", "")
        body = json.dumps(compile_kernel(source)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(port: int = 8017) -> None:
    """Serve the playground on `port` until interrupted."""
    print(f"playground on http://127.0.0.1:{port}  (ctrl-c to stop)")
    HTTPServer(("127.0.0.1", port), _Handler).serve_forever()


def render(source: str, out=None) -> str:
    """Every panel for `source` as plain text, for a terminal rather than a page."""
    got = compile_kernel(source)
    buf = out or io.StringIO()
    if got["error"]:
        print(got["error"], file=buf)
        return buf.getvalue() if out is None else ""
    for name in PANELS:
        print(f"\n{'=' * 78}\n{name.upper()}\n{'=' * 78}", file=buf)
        print(got[name], file=buf)
    return buf.getvalue() if out is None else ""
