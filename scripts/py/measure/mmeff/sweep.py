"""The contraction knobs on the Verilated v9t6s model, per call.

    python scripts/py/measure/mmeff/sweep.py --output DIR [--budget S] [--offline]

A setting's calls are the launches of each workload's `TinyJit` capture
pass (the kernels its replays run) on an offline device, under each of
PAIRINGS. Each knob's values are captured over its base (every knob off
but REQUIRES); a value's cycles are the sum over the launches it adds or
drops against its base, call-weighted: derived, not a model run of the
whole pass. A part (one value, workload and pairing: the launches it
drops and adds) is measured whole on the model (REPEAT runs, the last
counted), heaviest first by the per-kind proxy fit, up to `--coverage`
of each knob's moved cycles; the other parts are derived (proxy fit). A
value other than the base's is the default only when its measured parts
win beyond the warm-run noise and its derived parts predict no loss. A
product's inputs are drawn per contraction, so its logical output must
match across kernels of one `nk`.

Each invocation captures, then measures, for `--budget` seconds, keeps
every capture and kernel record in DIR/state.json and exits MORE; the
one that completes writes DIR/calls.json and exits 0. `--offline` stops
before the model, counting the kernels left to measure; `--report` writes
DIR/calls.json over the parts measured so far (the kernels it left
unmeasured in `left_unmeasured`).
"""

import argparse
import hashlib
import json
import os
import sys
import time
from collections import Counter
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import numpy as np
from examples.tinygrad import dispatch_measure
from examples.tinygrad.mnist_mlp import train_step
from examples.tinygrad.mnist_models import LeNet
from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.ktpugrad import Inference
from kohakutpu.application.ktpugrad.nn import Adam16, place
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import generated
from kohakutpu.application.runtime.card import WALL_ALARM
from kohakutpu.stack import rs
from tinygrad import Tensor
from tinygrad.engine.jit import TinyJit
from tinygrad.helpers import Context
from tinygrad.nn.state import get_parameters

ROOT = Path(__file__).resolve().parents[4]
BOARD = "vlt_v9t6s_1n"
BUILD = ROOT / "build/v9t6s/vlt_card_v9t6s_1n"
#: Runs of each kernel: the first cold (discarded), the last counted.
REPEAT = 2
#: Kernels run with one more warm run, the sample the warm-run spread (the
#: noise a knob's delta is held to) is taken from.
SAMPLE = 12
#: The share of each knob's moved cycles (by the proxy fit) its measured
#: parts cover; the rest is derived (proxy fit).
COVERAGE = 0.8
#: Measured kernels each kind's fit needs before it stands alone.
FIT_POINTS = 8
#: Wall seconds one kernel's runs may take (a call stays under 100 s).
CALL_SECONDS = 90
#: Each integer knob's values; a flag's are (False, True).
VALUES = {"n_tiles": (1, 2, 4), "ring_depth": (0, 1, 2), "quant_ring": (0, 1, 2)}
#: The knobs a knob acts under (`matmul::ring_resolve`, `quant_ring`'s
#: resolve): its values are measured over every other knob off but these.
REQUIRES = {
    "split_vc": {"ring_depth": 2},
    "tail_bands": {"ring_depth": 2},
    "quant_ring": {"ring_depth": 2, "activation_fp16": True},
}
#: The MXFP7 pairings a setting is measured under: the board's (the mover's
#: quant, one K block an entry) and vector (K pairs on the vector cores).
PAIRINGS = {"board": {}, "vector": {"KTPU_MX7_PAIRING": "vector"}}
#: The pass of a workload whose launches are counted: `TinyJit` (under
#: `Inference` too) runs the first call, captures the second.
CAPTURED = 1
#: Each workload's size. ViT-tiny's blocks launch the same kernels, so one
#: block weighs them as twelve do; the padded product's M is 2 x 197 (no
#: dividing tile balances its M tiles, `pad_m`).
VIT_BLOCKS = 1
LENET_BATCH = 16
PADM = (394, 192, 192)
WORKLOAD_SHAPES = {
    "vit": {"blocks": VIT_BLOCKS, "image": 224, "width": 192, "batch": 1},
    "lenet": {"batch": LENET_BATCH},
    "padm": dict(zip("mnk", PADM)),
}
ITEM = {"f16": 2, "f32": 4, "u32": 4, "u8": 1}
#: Seconds one `calls` invocation captures and measures before it saves its
#: state and exits with MORE; rerun it until it exits 0.
BUDGET = 60
MORE = 3


def kernel_id(source):
    return hashlib.sha1(source.encode()).hexdigest()[:16]


def vit(rng):
    model, shape = dispatch_measure.workload("vit", VIT_BLOCKS, 224, 192, 784, 128, 1)
    place(model)
    run = Inference(lambda x: model(x).realize(), replay=True)

    def one():
        x = rng.standard_normal(shape).astype(np.float16)
        return run(Tensor(x, device="KTPU").realize())

    return one


def lenet(rng):
    Tensor.manual_seed(0)
    with Context(DEV="CPU"):
        model = LeNet()
    place(model)
    opt = Adam16(get_parameters(model), lr=2e-3)
    step = TinyJit(lambda xb, yb: train_step(model, opt, xb, yb))
    eye = np.eye(10, dtype=np.float16)

    def one():
        xb = rng.random((LENET_BATCH, 784)).astype(np.float16)
        yb = eye[rng.integers(0, 10, LENET_BATCH)]
        with Context(TRAINING=1):
            _, logits = step(
                Tensor(xb, device="KTPU").realize(), Tensor(yb, device="KTPU").realize()
            )
        return logits

    return one


def padm(rng):
    m, n, k = PADM
    w = Tensor(rng.standard_normal((n, k)).astype(np.float16), device="KTPU")
    run = Inference(lambda a: (a @ w.T).realize(), replay=True)

    def one():
        a = rng.standard_normal((m, k)).astype(np.float16)
        return run(Tensor(a, device="KTPU").realize())

    return one


WORKLOADS = {"vit": vit, "lenet": lenet, "padm": padm}


def values(name):
    return VALUES.get(name, (False, True))


def environ(setting):
    """`setting` as the `KTPU_MM_*` overrides."""
    return {MM.KNOBS[k][0]: str(int(v)) for k, v in setting.items()}


def capture(workload, env):
    """`workload`'s counted pass's launches under the overrides `env`, in
    launch order: `(source, kind, made)`, `made` a generated kernel's
    `(generator, arguments)`, else None."""
    launched, counted, made, sources = [], [False], {}, {}
    graph, kernel, submit = generated.graph, D.Device.kernel, D.Device.submit

    def generate(generator, arguments, machine, mutant):
        source = graph(generator, arguments, machine, mutant)
        made[source] = (generator, arguments)
        return source

    def compiled(self, source, *a, **kw):
        c = kernel(self, source, *a, **kw)
        sources[id(c)] = source
        return c

    def submitted(self, c, binds, kind="dsl", info=None):
        if counted[0]:
            # A DSL kernel `specialise` compiled itself holds its trace.
            source = sources.get(id(c)) or c.traced.graph_json
            launched.append((source, kind, made.get(source)))
        return submit(self, c, binds, kind, info)

    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, env))
        stack.enter_context(patch.object(generated, "graph", generate))
        stack.enter_context(patch.object(D.Device, "kernel", compiled))
        stack.enter_context(patch.object(D.Device, "submit", submitted))
        dev = D.Device("offline", board=BOARD, nodes=1)
        try:
            with D.attached(dev):
                one = WORKLOADS[workload](np.random.default_rng(18))
                for index in range(CAPTURED + 1):
                    counted[0] = index == CAPTURED
                    one()
                    dev.drain()
        finally:
            dev.close()
    return launched


def layout(spec):
    return ", ".join(f"{k}={v['int']}" for k, v in spec["layout"])


def nbytes(spec, mt):
    shape = spec["shape"]
    if spec["dtype"].startswith("mx7"):
        return len(pack(spec, np.zeros(shape, np.float16), mt))
    return int(np.prod(shape)) * ITEM[spec["dtype"]]


def pack(spec, x, mt):
    """`x` packed as the port `spec` holds it (ktpu-l1 `tensor::pack`)."""
    rows, cols = spec["shape"]
    ty = f"{spec['dtype']}[{rows}, {cols}] @dram({layout(spec)})"
    return rs._rs.buffer_pack(ty, x.astype(np.float16).tobytes(), mt, None)


def contraction(args):
    """A product call's identity under every setting: its shape, epilogue and
    operands, not its knobs."""
    p = args["mm"]["made_by"]["args"]
    key = {k: p[k] for k in ("bt", "m", "n", "k", "shared")}
    key.update(e=args["e"], extras=args["extras"], pairing=p.get("pairing"))
    return json.dumps(key, sort_keys=True)


def seeded(key, *shape):
    seed = int(hashlib.sha1(key.encode()).hexdigest()[:8], 16)
    return np.random.default_rng(seed).standard_normal(shape).astype(np.float16)


def product_inputs(args, ports, mt):
    """Each input port's bytes of a product: the contraction's logical A
    [bt, m, k], B and epilogue operands, laid out to the setting's tiling."""
    plan = json.loads(rs._rs.mm_plan(json.dumps(args["mm"]["made_by"]["args"]), mt))
    bt, m, n, k, mp, kp = (plan[x] for x in ("bt", "m", "n", "k", "mp", "kp"))
    key = contraction(args)
    nb = n if plan["shared"] else bt * n
    a, b = seeded(key + "a", bt, m, k), seeded(key + "b", nb, k)
    data = {}
    for p in ports:
        spec, name = p["spec"], p["name"]
        shape = spec["shape"]
        if name == "a" and spec["dtype"] == "f16":
            data[name] = a.reshape(bt * m, k).tobytes()
        elif name == "a":
            x = np.zeros((bt, mp, kp), np.float16)
            x[:, :m, :k] = a
            data[name] = pack(spec, x.reshape(bt * mp, kp), mt)
        elif name == "b":
            x = np.zeros((nb, kp), np.float16)
            x[:, :k] = b
            data[name] = pack(spec, x, mt)
        elif name.startswith("in"):
            o = args["extras"][int(name[2:]) - 1]
            if o["kind"] in ("full", "row"):
                rows, cols = shape
                x = np.zeros((bt * mp, cols), np.float16)
                v = seeded(key + name, bt, m, cols)
                for i in range(bt):
                    x[i * mp : i * mp + m] = v[i]
                data[name] = x[:rows].tobytes()
            else:
                data[name] = seeded(key + name, *shape).tobytes()
    return data, (bt, m, n, mp, plan["nk"])


def logical_out(raw, bt, m, n, mp, pitch):
    out = np.frombuffer(raw, np.float16).reshape(-1, pitch)
    return b"".join(out[i * mp : i * mp + m, :n].tobytes() for i in range(bt))


def measure(dev, source, kind, made, mt, out_dir, runs):
    """One launched kernel on the model: `runs` runs' cycles, its package
    bytes, and a product's logical output."""
    kid = kernel_id(source)
    output = rs._rs.compile(source, mt, fetch=True)
    assert not output["verdict"]["refused"], (kid, output["verdict"])
    graph = json.loads(source)
    (out_dir / f"{kid}.graph.json").write_text(source, encoding="utf-8")
    (out_dir / f"{kid}.l1").write_text(output["l1"], encoding="utf-8")
    comp = generated.from_output(dev, graph, output)
    generator, args = made if made else (None, None)
    args = args and json.loads(args)
    ports = graph["ports"]
    inputs = [p for p in ports if p["role"] == "in"]
    if generator == "matmul.mm_kernel":
        data, shape = product_inputs(args, inputs, mt)
    else:
        data = {
            p["name"]: seeded(
                kid + p["name"], int(np.prod(p["spec"]["shape"]))
            ).tobytes()
            for p in inputs
            if p["spec"]["dtype"] == "f16"
        }
    binds = {p["name"]: dev.alloc(nbytes(p["spec"], mt)) for p in ports}
    try:
        for p in ports:
            size = nbytes(p["spec"], mt)
            raw = data.get(p["name"], b"")
            fill = bytes([0xD3]) if p["role"] == "out" else bytes(1)
            dev.write(binds[p["name"]], raw + fill * (size - len(raw)))
        cycles = [dev.run(comp, binds, kind=kind).cycles for _ in range(runs)]
        record = {
            "id": kid,
            "kind": kind,
            "generator": generator,
            "arguments": args,
            "name": graph["name"],
            "cycles": cycles,
            "package_bytes": sum(len(p["bytes"]) for p in output["packages"].values()),
        }
        if generator == "matmul.mm_kernel":
            out = next(p for p in ports if p["name"] == "out")
            raw = dev.read(binds["out"], nbytes(out["spec"], mt))
            bt, m, n, mp, nk = shape
            got = logical_out(raw, bt, m, n, mp, out["spec"]["shape"][1])
            record.update(
                contraction=contraction(args),
                nk=nk,
                output_sha1=hashlib.sha1(got).hexdigest(),
            )
        return record
    finally:
        for at in binds.values():
            dev.free(at)


def settings():
    """Each knob's base (every knob off but the knobs it acts under) and its
    values over that base: `(knob, base, {value: setting})`."""
    off = {k: values(k)[0] for k in MM.KNOBS}
    for name in MM.KNOBS:
        base = {**off, **REQUIRES.get(name, {})}
        yield name, base, {v: {**base, name: v} for v in values(name)}


def changed(base, on):
    """The launches of capture `on` and of capture `base` that are not in
    both, as two multisets of kernel ids: all the cycles the change moves."""
    a, b = Counter(k for k, _, _ in base), Counter(k for k, _, _ in on)
    return a - b, b - a


def work_of(kid, made, out, mt):
    """A kernel's kind and offline work: a product's MACs (bt * mp * n * kp
    of its plan), else the elements of its largest input port."""
    kind, call = made[kid]
    if call is not None and call[0] == "matmul.mm_kernel":
        args = json.loads(call[1])["mm"]["made_by"]["args"]
        plan = json.loads(rs._rs.mm_plan(json.dumps(args), mt))
        return "product", plan["bt"] * plan["mp"] * plan["n"] * plan["kp"]
    graph = json.loads(
        (out / "kernels" / f"{kid}.graph.json").read_text(encoding="utf-8")
    )
    ins = [p for p in graph["ports"] if p["role"] == "in"]
    return kind, max(int(np.prod(p["spec"]["shape"])) for p in ins)


def rank(xs):
    r = np.empty(len(xs))
    r[np.argsort(xs, kind="stable")] = np.arange(len(xs))
    return r


def fit(works, kernels):
    """Per kind, cycles = a * work + b by least squares over the measured
    kernels (the warm run), with its Spearman rho and relative RMS residual."""
    out = {}
    for kind in sorted({k for k, _ in works.values()}):
        pts = [
            (works[kid][1], r["cycles"][-1])
            for kid, r in kernels.items()
            if works[kid][0] == kind
        ]
        x, y = np.array([p[0] for p in pts], float), np.array(
            [p[1] for p in pts], float
        )
        a, b = np.polyfit(x, y, 1) if len(pts) > 1 else (y.sum() / max(x.sum(), 1), 0.0)
        resid = y - (a * x + b)
        out[kind] = {
            "a": float(a),
            "b": float(b),
            "points": len(pts),
            "rho": float(np.corrcoef(rank(x), rank(y))[0, 1]) if len(pts) > 2 else None,
            "rms_relative": float(np.sqrt(np.mean((resid / y) ** 2))) if pts else None,
        }
    return out


def estimator(works, kernels, fits):
    """A kernel's cycles: its warm run where measured, else its kind's fit."""

    def est(kid):
        if kid in kernels:
            return kernels[kid]["cycles"][-1]
        kind, w = works[kid]
        return max(fits[kind]["a"] * w + fits[kind]["b"], 0.0)

    return est


def parts_of(base, by_value, captures):
    """A knob's changed parts: per value, workload and pairing, the
    launches the value drops and adds (`changed`), keyed `value/w/p`."""
    return {
        f"{v}/{w}/{p}": changed(
            captures[part_key(base, w, p)], captures[part_key(setting, w, p)]
        )
        for v, setting in by_value.items()
        for w in WORKLOADS
        for p in PAIRINGS
    }


def moved(name, base, by_value, captures):
    """Every launch a knob's values add or drop, over every workload and
    pairing: kernel id -> launches."""
    out = Counter()
    for gone, new in parts_of(base, by_value, captures).values():
        out += gone + new
    return out


def covering(parts, est, coverage, cap):
    """The kernels of the heaviest parts (fitted cycles of the launches
    dropped and added) that cover `coverage` of the knob's moved cycles. A
    part is measured whole, both sides of its delta; a part holding a
    kernel whose fitted cycles pass `cap` (a run too long for one call) is
    left derived but still counts in the total."""
    weight = {key: cycles_of(g + n, est) for key, (g, n) in parts.items()}
    total, got, out = sum(weight.values()), 0.0, set()
    for key in sorted(weight, key=lambda key: -weight[key]):
        if got >= coverage * total:
            break
        gone, new = parts[key]
        if all(est(k) <= cap for k in gone + new):
            out |= set(gone + new)
            got += weight[key]
    return out


def rate_of(state):
    """Model cycles a wall second over the invocations so far (every run of
    every measured kernel over the model invocations' wall time, bring-up
    and compiles in it)."""
    runs = sum(sum(r["cycles"]) for r in state["kernels"].values())
    return runs / state["model_seconds"]


def cap_of(state):
    """The most warm cycles a kernel may take for its compile and runs to
    stay within CALL_SECONDS: by a fit of wall seconds to warm cycles over
    the kernels timed alone, else by the overall rate."""
    pts = [
        (r["cycles"][-1], r["seconds"])
        for r in state["kernels"].values()
        if "seconds" in r
    ]
    if len(pts) > 1:
        c, d = np.polyfit([p[0] for p in pts], [p[1] for p in pts], 1)
        if c > 0:
            return (CALL_SECONDS - d) / c
    return rate_of(state) * CALL_SECONDS / REPEAT


def cycles_of(ids, est):
    return sum(est(k) * n for k, n in ids.items())


def spread_of(kernels):
    """The warm-run spread: the largest relative difference of a sampled
    kernel's last two (warm) runs."""
    return max(
        abs(r["cycles"][-1] - r["cycles"][-2]) / r["cycles"][-1]
        for r in kernels.values()
        if len(r["cycles"]) > REPEAT
    )


def knob_report(name, base, by_value, captures, kernels, est, spread, residual, cap):
    """One knob's values against its base, per workload/pairing: the cycles
    of the launches that differ, summed over the capture's call counts. A
    part whose dropped and added kernels all ran on the model (the warm
    run) is measured; any other part is derived (proxy fit) whole, its
    noise `residual` of its fitted cycles and `spread` of its measured
    ones. A value other than the base's is the default only when it wins;
    with no winner, or with kernels over `cap` holding over half the moved
    cycles, the base's value stays, pending an owner-approved run."""
    kernels_moved = moved(name, base, by_value, captures)
    total = cycles_of(kernels_moved, est)
    rows, covered = {}, Counter()
    for key, (gone, new) in parts_of(base, by_value, captures).items():
        v, part = key.split("/", 1)
        both = gone + new
        fitted = Counter({k: n for k, n in both.items() if k not in kernels})
        delta = cycles_of(new, est) - cycles_of(gone, est)
        side = "derived" if fitted else "measured"
        if not fitted:
            covered += both
        rows.setdefault(v, {"parts": {}})["parts"][part] = {
            "side": side,
            "measured_delta": delta if side == "measured" else 0.0,
            "derived_delta": delta if side == "derived" else 0.0,
            "measured_noise": (
                spread * cycles_of(both, est) if side == "measured" else 0.0
            ),
            "derived_noise": (
                residual * cycles_of(fitted, est)
                + spread * cycles_of(both - fitted, est)
                if side == "derived"
                else 0.0
            ),
            "kernels": [sum(gone.values()), sum(new.values())],
        }
    covered = cycles_of(covered, est)
    for row in rows.values():
        for k in ("measured_delta", "derived_delta", "measured_noise", "derived_noise"):
            row[k] = sum(x[k] for x in row["parts"].values())
        row["delta"] = row["measured_delta"] + row["derived_delta"]
        # A value wins only on the measured set, beyond its warm noise, and
        # where the derived remainder predicts no loss.
        row["wins"] = (
            row["measured_delta"] < -row["measured_noise"] and row["derived_delta"] <= 0
        )
    over = cycles_of(
        Counter({k: n for k, n in kernels_moved.items() if est(k) > cap}), est
    )
    winners = [v for v in by_value if v != base[name] and rows[str(v)]["wins"]]
    best = min(winners, key=lambda v: rows[str(v)]["measured_delta"], default=None)
    pending = best is None or over > total / 2
    return {
        "knob": name,
        "base": base,
        "values": rows,
        "covered_fraction": covered / total if total else 1.0,
        "over_cap_fraction": over / total if total else 0.0,
        "winner": base[name] if pending else best,
        "pending_owner_run": pending and bool(kernels_moved),
        "changes_no_kernel": all(
            x["kernels"] == [0, 0] for r in rows.values() for x in r["parts"].values()
        ),
    }


def works_of(plan, captures, made, out, mt):
    """Every moved kernel's kind and offline work."""
    ids = set()
    for n, b, v in plan:
        ids |= set(moved(n, b, v, captures))
    return {kid: work_of(kid, made, out, mt) for kid in ids}


def to_measure(plan, captures, kernels, made, out, mt, coverage, cap):
    """The unmeasured kernels each knob's `coverage` takes (by the current
    fit, none over `cap` fitted cycles), and, for a kind with fewer than
    FIT_POINTS measured, its heaviest moved kernels under the cap up to
    that many."""
    works = works_of(plan, captures, made, out, mt)
    est = estimator(works, kernels, fit(works, kernels))
    pick = set()
    for n, b, v in plan:
        pick |= covering(parts_of(b, v, captures), est, coverage, cap)
    for kind in {k for k, _ in works.values()}:
        have = [kid for kid in kernels if works.get(kid, ("",))[0] == kind]
        rest = sorted(
            (
                kid
                for kid in works
                if works[kid][0] == kind and kid not in kernels and est(kid) <= cap
            ),
            key=lambda kid: -est(kid),
        )
        pick |= set(rest[: max(FIT_POINTS - len(have), 0)])
    todo = sorted(pick - set(kernels), key=lambda kid: -est(kid))
    over = sorted(
        (kid for kid in works if kid not in kernels and est(kid) > cap),
        key=lambda kid: -est(kid),
    )
    return todo, sum(est(k) for k in todo), [(k, works[k], est(k)) for k in over]


def part_key(setting, workload, pairing):
    return json.dumps([setting, workload, pairing], sort_keys=True)


class OutOfTime(Exception):
    """The invocation's budget ran out before its next capture or kernel."""


def calls(args):
    out = args.output
    (out / "kernels").mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    deadline = started + args.budget
    state_path = out / "state.json"
    state = (
        json.loads(state_path.read_text(encoding="utf-8"))
        if state_path.exists()
        else {"kernels": {}, "captures": {}, "seconds": 0.0, "invocations": 0}
    )
    # kernels: id -> record; captures: part key -> [[id, kind, made], ..]
    kernels, captures = state["kernels"], state["captures"]
    opened = []

    def model():
        if not opened:
            dev = D.Device(
                kind="sim",
                board=BOARD,
                nodes=1,
                build=args.build,
                log=out / "calls.model.log",
                classes=False,
            )
            dev.card.alarm = args.alarm
            opened.append(dev)
        return opened[0]

    model_before = state.get("model_seconds", 0.0)

    def spend():
        # Each finished capture or kernel is on disk before the next starts:
        # a run cut by its timeout loses only the piece in flight.
        if opened:
            state["model_seconds"] = model_before + time.monotonic() - started
        state_path.write_text(json.dumps(state) + "\n", encoding="utf-8")
        if time.monotonic() > deadline:
            raise OutOfTime

    offline = D.Device("offline", board=BOARD, nodes=1)
    mt = rs.machine_json(offline.machine)
    offline.close()
    plan = list(settings())
    parts = {
        part_key(s, w, p): (s, w, p)
        for _, base, by_value in plan
        for s in [base, *by_value.values()]
        for w in WORKLOADS
        for p in PAIRINGS
    }
    made = state.setdefault("made", {})
    try:
        for key, (setting, workload, pairing) in parts.items():
            if key in captures:
                continue
            spend()
            launched = capture(workload, {**environ(setting), **PAIRINGS[pairing]})
            for source, kind, call in launched:
                kid = kernel_id(source)
                path = out / "kernels" / f"{kid}.graph.json"
                if not path.exists():
                    path.write_text(source, encoding="utf-8")
                made[kid] = [kind, call]
            captures[key] = [[kernel_id(s), k, c] for s, k, c in launched]
            print(f"captured {key}: {len(launched)} launches", flush=True)

        cap = cap_of(state)
        todo, cycles, over = to_measure(
            plan, captures, kernels, made, out, mt, args.coverage, cap
        )
        state["over_cap"] = over
        print(
            f"{len(todo)} to measure at coverage {args.coverage}: {cycles:.0f} fitted"
            f" cycles, {cycles * REPEAT / rate_of(state):.0f} s; cap {cap:.0f} cycles,"
            f" {len(over)} kernels over it",
            flush=True,
        )
        if args.offline:
            return MORE if todo else 0
        state["left"] = len(todo)
        for kid in [] if args.report else todo:
            spend()
            source = (out / "kernels" / f"{kid}.graph.json").read_text(encoding="utf-8")
            kind, call = made[kid]
            sampled = sum(len(r["cycles"]) > REPEAT for r in kernels.values())
            runs = REPEAT + (sampled < SAMPLE)
            dev = model()
            begun = time.monotonic()
            kernels[kid] = measure(dev, source, kind, call, mt, out / "kernels", runs)
            kernels[kid]["seconds"] = time.monotonic() - begun
            r = kernels[kid]
            print(
                json.dumps({k: r[k] for k in ("id", "name", "cycles", "seconds")}),
                flush=True,
            )
    except OutOfTime:
        print(f"MORE: {len(captures)} captures, {len(kernels)} kernels", flush=True)
        return MORE
    finally:
        for dev in opened:
            dev.close()
        state["seconds"] += time.monotonic() - started
        if opened:
            state["model_seconds"] = model_before + time.monotonic() - started
        state["invocations"] += 1
        state_path.write_text(json.dumps(state) + "\n", encoding="utf-8")

    spread = spread_of(kernels)
    works = works_of(plan, captures, made, out, mt)
    fits = fit(works, kernels)
    est = estimator(works, kernels, fits)
    residual = max(f["rms_relative"] for f in fits.values() if f["points"])
    cap = cap_of(state)
    knobs = [
        knob_report(n, b, v, captures, kernels, est, spread, residual, cap)
        for n, b, v in plan
    ]
    parity = {}
    for r in kernels.values():
        if "output_sha1" in r:
            parity.setdefault(f"{r['contraction']} nk={r['nk']}", set()).add(
                r["output_sha1"]
            )
    report = {
        "board": BOARD,
        "workloads": {w: WORKLOAD_SHAPES[w] for w in WORKLOADS},
        "captured": CAPTURED,
        "pairings": PAIRINGS,
        "repeat": REPEAT,
        "coverage": args.coverage,
        "left_unmeasured": state["left"] if args.report else 0,
        "fits": fits,
        "over_cap": state.get("over_cap", []),
        "warm_spread": spread,
        "warm_sample": [
            {"id": r["id"], "name": r["name"], "cycles": r["cycles"]}
            for r in kernels.values()
            if len(r["cycles"]) > REPEAT
        ],
        "requires": REQUIRES,
        "defaults": {k["knob"]: k["winner"] for k in knobs},
        "knobs": knobs,
        "captures": {key: captures[key] for key in parts},
        "kernels": list(kernels.values()),
        "parity": {k: sorted(v) for k, v in parity.items()},
        "parity_right": all(len(v) == 1 for v in parity.values()),
        "counts": {
            "kernels": len(kernels),
            "captures": len(parts),
            "contractions": len(
                {r.get("contraction") for r in kernels.values()} - {None}
            ),
        },
        "invocations": state["invocations"],
        "wall_seconds": state["seconds"],
        "model_seconds": state.get("model_seconds", 0.0),
    }
    (out / "calls.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    assert report["parity_right"], report["parity"]
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--build", type=Path, default=BUILD)
    ap.add_argument("--budget", type=float, default=BUDGET)
    ap.add_argument("--coverage", type=float, default=COVERAGE)
    ap.add_argument(
        "--offline",
        action="store_true",
        help="capture only and count the kernels the knobs change",
    )
    ap.add_argument(
        "--report",
        action="store_true",
        help="measure nothing: write DIR/calls.json over the parts measured so far",
    )
    ap.add_argument(
        "--alarm",
        type=float,
        default=WALL_ALARM,
        help="wall seconds a kernel's wait runs on the model before it fails",
    )
    return calls(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
