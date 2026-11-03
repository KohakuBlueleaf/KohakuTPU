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
import hashlib
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


#: The byte every output is painted with before a run: an output a unit never
#: wrote reads back as this, never as an earlier run's result.
POISON = 0xA5

#: What a result keeps of each sysnode's `card.run` result.
KEPT = ("cycles", "phases", "steps", "fault")


def place(card, layout, inputs: dict, outputs=(), node: int | None = None) -> dict:
    """Each parameter of `layout` placed on `card` (staging: its `node` layout key,
    else `node`): `inputs` written, `outputs` left unwritten, the rest zero."""
    binds = {}
    for p, b in layout:
        at = b.layout["node"] if b.layout["node"] is not None else node
        if p in outputs:
            binds[p] = card.place(b.nbytes, b.space, at)
            continue
        data = raw(b, inputs[p]) if p in inputs else bytes(b.nbytes)
        binds[p] = card.put(np.frombuffer(data, np.uint8), b.space, at)
    return binds


def paint(card, binds: dict, outs) -> None:
    """Every output of `outs` (``[(param, Buffer)]``) painted with POISON."""
    for p, b in outs:
        card.write(binds[p], bytes([POISON]) * b.nbytes)


def sysnodes(res: dict, node: int) -> dict:
    """Each sysnode's `KEPT` of a `card.run` result; a one-package run's is `node`'s."""
    per = res.get("nodes", {node: res})
    return {n: {k: r[k] for k in KEPT if k in r} for n, r in per.items()}


def digest(out: dict) -> str:
    """sha1 of the outputs' bytes, in name order."""
    h = hashlib.sha1()
    for k in sorted(out):
        h.update(np.ascontiguousarray(out[k]).tobytes())
    return h.hexdigest()[:12]


def deviation(got, ref) -> tuple:
    """|got - ref| elementwise, and that over the reference's largest magnitude."""
    got = np.asarray(got, np.float64).ravel()
    ref = np.asarray(ref, np.float64).ravel()[: got.size]
    a = np.abs(got - ref)
    return a, a / max(np.abs(ref).max(), 1e-30)


def score(out: dict, want: dict) -> dict:
    """Relative error of every output against the reference: p50, p99, max."""
    e = np.concatenate([deviation(out[k], ref)[1] for k, ref in want.items()])
    return {
        "p50": float(np.percentile(e, 50)),
        "p99": float(np.percentile(e, 99)),
        "max": float(e.max()),
    }


def errors(got, ref) -> dict:
    """One output's absolute and relative (`deviation`) error: max, mean, median."""
    stats = (("max", np.max), ("mean", np.mean), ("median", np.median))
    return {
        f"{kind}_{name}": float(f(e))
        for kind, e in zip(("abs", "rel"), deviation(got, ref))
        for name, f in stats
    }


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
    bytes}, "cycles", "cold", "nodes", "phases", "pkg"}`` (``"nodes"``: each
    sysnode's `KEPT`, `sysnodes`), on a debug card
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
    layout = bufs(m, fn, card.machine_json)
    binds = place(card, layout, inputs)
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
        "nodes": sysnodes(last, card.nodes[0]),
        "phases": last.get("phases", {}),
        "debug": last.get("debug"),
        "dump": last.get("dump"),
        "pkg": last.get("pkg"),
    }


def seeded(b: Buffer, rng):
    """Seeded inputs: a float buffer uniform in +-1/sqrt(fan-in), its last axis (a
    linear layer's initial weights), small integers otherwise."""
    if b.dtype.startswith("mx7") or b.dtype in ("f16", "f32"):
        bound = 1 / np.sqrt(b.host_shape[-1])
        return rng.uniform(-bound, bound, b.host_shape).astype(b.host_dtype)
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
