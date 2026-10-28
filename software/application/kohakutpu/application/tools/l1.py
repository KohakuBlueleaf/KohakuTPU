"""Run a standalone hand-written L1 example (`examples/l1/**/NAME.ktpu`) on a
Verilated card: every parameter becomes a buffer, the given inputs are
written, the rest zeroed, the body runs (on every node its `node` blocks
name), and every buffer is read back.

    python -m kohakutpu.application.tools.l1 --build MODEL [--nodes 0,1] NAME [--out DIR]

NAME is an example's name, or a path ending ``.ktpu``.

The CLI fills each input with seeded values by its dtype and reports cycles;
a test calls :func:`run` with its own inputs and checks the outputs.
"""

import argparse
import json
import pathlib
import sys
import tempfile

import numpy as np

from kohakutpu.application.runtime import packages as verify
from kohakutpu.application.runtime.card import VerilatorCard
from kohakutpu.language import examples
from kohakutpu.stack import kernel
from kohakutpu.stack.buffer import Buffer


def bufs(m, name: str, machine_json: str) -> list:
    """``[(param, Buffer)]`` of `fn name.l1`."""
    return [(p, Buffer.of(t, machine_json)) for p, t in m.body(name, "l1").params]


def raw(b: Buffer, x) -> bytes:
    """`x` (an array in the host's form, or bytes) as the buffer's bytes."""
    if isinstance(x, (bytes, bytearray)):
        return bytes(x)
    return b.pack(x)


def run(
    card,
    name: str,
    inputs: dict,
    fn: str | None = None,
    reps: int = 2,
    **run,
) -> dict:
    """Run example `name` on `card` `reps` times (the last is timed) with
    `inputs` (param -> array or bytes; the rest zero), the same memory each
    time: an example reruns to the same result. `run` goes to every
    `card.run` (``trace``, ``flags``). Returns ``{"out": {param: array or
    bytes}, "cycles", "cold", "nodes", "phases", "pkg"}``, on a debug card
    the last run's decoded records under ``"debug"``, and with ``dump`` its
    buffer dumps under ``"dump"``; the buffers are freed."""
    m = examples.load(examples.example(name))
    fn = fn or m.names()[0]
    return run_compiled(
        card, kernel.compile(m, fn, card.machine, level="l1"), inputs, reps, **run
    )


def run_compiled(card, compiled, inputs: dict, reps: int = 2, **run) -> dict:
    """:func:`run` of Rust packages held by `stack.kernel.Compiled`."""
    held = card.mark()
    try:
        return _run(
            card, compiled.l1, compiled.name, inputs, reps, run, compiled.programs
        )
    finally:
        card.release(held)


def _run(card, m, fn, inputs, reps, run, programs) -> dict:
    """`programs(binds)`: what the card runs."""
    binds, layout = {}, bufs(m, fn, card.machine_json)
    for p, b in layout:
        data = raw(b, inputs[p]) if p in inputs else bytes(b.nbytes)
        node = b.layout.get("node")
        binds[p] = card.put(np.frombuffer(data, np.uint8), b.space, node)
    run = dict(run or {})
    if run.get("dump") is True:
        # Every parameter, by name (DBG.DUMP.001).
        run["dump"] = {p: (binds[p], b.nbytes) for p, b in layout}
    progs = programs(binds)
    single = list(progs) == [None]
    got = [
        card.run(progs[None] if single else progs, **(run or {})) for _ in range(reps)
    ]
    out = {}
    for p, b in layout:
        data = card.get(binds[p], b.nbytes)
        out[p] = data if b.dtype.startswith("mx7") else b.unpack(data)
    last = got[-1]
    return {
        "out": out,
        "cycles": last["cycles"],
        "cold": got[0]["cycles"],
        "nodes": {n: r["cycles"] for n, r in last.get("nodes", {}).items()},
        "phases": last.get("phases", {}),
        "debug": last.get("debug"),
        "dump": last.get("dump"),
        "pkg": last.get("pkg"),
    }


def seeded(b: Buffer, rng):
    """CLI inputs: normal values in a float buffer, small integers otherwise."""
    if b.dtype.startswith("mx7") or b.dtype in ("f16", "f32"):
        return rng.standard_normal(b.host_shape).astype(b.host_dtype)
    return rng.integers(0, 8, b.host_shape).astype(b.host_dtype)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", required=True)
    ap.add_argument("--board", help="required board name, or KOHAKUTPU_BOARD")
    ap.add_argument("--nodes", default="0")
    ap.add_argument("--fn", default=None)
    ap.add_argument("--seed", type=int, default=2)
    ap.add_argument("--out", default=None)
    ap.add_argument(
        "--check",
        default="refuse",
        choices=verify.GATES,
        help="the static checker before the run",
    )
    ap.add_argument("name")
    a = ap.parse_args()
    nodes = tuple(int(n) for n in a.nodes.split(","))
    m = examples.load(examples.example(a.name))
    fn = a.fn or m.names()[0]
    rng = np.random.default_rng(a.seed)
    # The model's log holds the counters: one per run, or runs on one build mix.
    out = pathlib.Path(a.out or tempfile.mkdtemp(prefix="ktpu_l1_"))
    out.mkdir(parents=True, exist_ok=True)
    stem = pathlib.Path(a.name).stem
    card = VerilatorCard(
        a.build,
        board=a.board,
        nodes=nodes,
        log=out / f"{stem}.model.log",
        check=a.check,
    )
    inputs = {p: seeded(b, rng) for p, b in bufs(m, fn, card.machine_json)}
    res = run(card, a.name, inputs, fn)
    stats = card.close()
    beats = sum(s.get("vres", {}).get("lanes", 0) for s in stats.values()) // 2
    report = {
        "example": a.name,
        "cycles": res["cycles"],
        "cold": res["cold"],
        "nodes": res["nodes"],
        "lane_beats": beats,
        "phases": res["phases"],
    }
    print(json.dumps(report))
    (out / f"{stem}.json").write_text(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
