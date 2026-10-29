"""Numerical card witnesses, with host references and Rust buffer packing."""

import importlib

import numpy as np
from harness.registry import mutant, patched, runner
from tinygrad.tensor import Tensor as TG

from kohakutpu.application.runtime import card as RC
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.call import call
from kohakutpu.application.tools import l1 as L1
from kohakutpu.language import examples
from kohakutpu.language.examples import dsl as DX
from kohakutpu.language.numerics import fp16, mxfp7
from kohakutpu.stack import kernel

TM = importlib.import_module("kohakutpu.application.api.tensor")


@mutant("num.card/readback-corrupt")
def readback_corrupt():
    keep = RC.NodeCard.get

    def get(self, at, nbytes):
        return bytes(b ^ 0x40 for b in bytes(keep(self, at, nbytes)))

    with patched(RC.NodeCard, "get", get):
        yield


def _float(v):
    return float.fromhex(v) if isinstance(v, str) else float(v)


def _hexes(a):
    return [float.hex(float(v)) for v in np.asarray(a, np.float64).reshape(-1)]


def _bits16(a):
    return [f"{int(b):04x}" for b in np.asarray(a, np.float16).view(np.uint16)]


def _rel(got, want):
    got, want = np.asarray(got, np.float64), np.asarray(want, np.float64)
    return float(np.abs(got - want).max() / max(np.abs(want).max(), 1e-9))


def _dev(ctx):
    return D.attach(ctx["session"].dev)


def _grid(bits, shape, fill):
    a = np.full(int(np.prod(shape)), fill, np.uint16)
    a[: len(bits)] = [int(b, 16) for b in bits]
    return a.view(np.float16).reshape(shape)


@runner("num.card.elementwise")
def card_elementwise(inp, ctx):
    _dev(ctx)
    shape = tuple(inp.get("shape", (64, 128)))
    x, y = _grid(inp["x"], shape, 0), _grid(inp["y"], shape, 0x3C00)
    a, b = TM.tensor(x), TM.tensor(y)
    got = (a * b if inp["op"] == "mul" else a + b).numpy()
    return _bits16(got.reshape(-1)[: len(inp["x"])])


@runner("num.card.rowsum")
def card_rowsum(inp, ctx):
    _dev(ctx)
    rng = np.random.default_rng(inp["seed"])
    x = rng.integers(-3, 4, inp["shape"]).astype(np.float16)
    got = TM.tensor(x).sum(-1).numpy()
    return {
        "exact": bool(
            np.array_equal(got.astype(np.float64), x.astype(np.float64).sum(-1))
        )
    }


@runner("num.card.linear")
def card_linear(inp, ctx):
    _dev(ctx)
    m, n, k = inp["m"], inp["n"], inp["k"]
    x, w = np.zeros((m, k), np.float16), np.zeros((n, k), np.float16)
    for a, entries in ((x, inp["x"]), (w, inp["w"])):
        for e in entries:
            a[e[0], e[1] : e[-2] if len(e) == 4 else e[1] + 1] = e[-1]
    got = TM.linear(TM.tensor(x), TM.tensor(w)).numpy()
    return _bits16([got[i, j] for i, j in inp["read"]])


def _normal(spec):
    rng = np.random.default_rng(spec["seed"])
    x = rng.standard_normal(spec["shape"]) * spec.get("scale", 1.0)
    return (x / spec.get("div", 1.0) + spec.get("offset", 0.0)).astype(np.float16)


@runner("num.card.product")
def card_product(inp, ctx):
    _dev(ctx)
    x, w = _normal(inp["x"]), _normal(inp["w"])
    got = TM.linear(TM.tensor(x), TM.tensor(w)).numpy()
    want = mxfp7.value_fp16(x) @ mxfp7.value_fp16(w).T
    return {"rel_below": _rel(got, want) < inp["rel"]}


@runner("num.card.tinygrad")
def card_tinygrad(inp, ctx):
    _dev(ctx)
    prog = inp["program"]
    if prog == "mul_ones":
        x = np.asarray([_float(v) for v in inp["x"]], inp["dtype"])
        x = np.resize(x, (16, 128)).astype(inp["dtype"])
        got = (TG(x, device="KTPU") * TG(np.ones_like(x), device="KTPU")).numpy()
        return {"values": _hexes(got.reshape(-1)[: len(inp["x"])])}
    x = _normal(inp["x"])
    if prog == "transpose":
        got = TG(x, device="KTPU").T.contiguous().numpy()
        return {"exact": bool(np.array_equal(got, x.T))}
    y = _normal(inp["y"])
    got = ((TG(x, device="KTPU") * 2 + TG(y, device="KTPU")).relu() - 1).numpy()
    want = np.maximum(x.astype(np.float64) * 2 + y, 0) - 1
    return {"rel_below": _rel(got, want) < inp["rel"]}


def _silu(v):
    v = np.asarray(v, np.float64)
    return v / (1 + np.exp(-v))


def _softmax(v):
    v = np.asarray(v, np.float64)
    e = np.exp(v - v.max(1, keepdims=True))
    return e / e.sum(1, keepdims=True)


def _layernorm(x, g, b):
    v = x.astype(np.float64)
    return (v - v.mean(1, keepdims=True)) / np.sqrt(
        v.var(1, keepdims=True) + 1e-5
    ) * g + b


REFS = {
    "add": ("out", lambda i: i["x"].astype(np.float64) + i["y"]),
    "silu": ("y", lambda i: _silu(i["x"])),
    "softmax": ("y", lambda i: _softmax(i["x"])),
    "layernorm": ("y", lambda i: _layernorm(i["x"], i["g"], i["b"])),
    "leaky": (
        "y",
        lambda i: np.where(i["x"] > 0, i["x"], 0.1 * i["x"].astype(np.float64)),
    ),
    "splitk": ("y", lambda i: mxfp7.value_fp16(i["x"]) @ mxfp7.value_fp16(i["w"]).T),
}


@runner("num.card.example")
def card_example(inp, ctx):
    card = _dev(ctx).card
    ins = {k: _normal(s) for k, s in inp["inputs"].items()}
    name = inp["name"]
    if inp["source"] == "l2":
        m = examples.load(examples.example(name, "l2"))
        c = kernel.compile(m, name, card.machine)
        layout = dict(L1.bufs(c.l1, name, card.machine_json))
        out = L1.run_compiled(card, c, ins, reps=1)["out"]
    else:
        m = examples.load(examples.example(name))
        layout = dict(L1.bufs(m, name, card.machine_json))
        out = L1.run(card, name, ins, reps=1)["out"]
    if name == "convert":
        x = ins["x"]
        return {
            "exact": bytes(out["a"]) == layout["a"].pack(x)
            and bytes(out["b"]) == layout["b"].pack(x)
            and bool(
                np.array_equal(
                    out["t"], fp16.values(x.view(np.uint16), consumer="cluster")
                )
            )
        }
    key, ref = REFS[name]
    return {"rel_below": _rel(out[key], ref(ins)) < inp["rel"]}


@runner("num.card.dsl")
def card_dsl(inp, ctx):
    dev = _dev(ctx)
    e = DX.EXAMPLES[inp["name"]]
    ins = e.inputs(np.random.default_rng(inp["seed"]))
    outs, _ = call(dev, e.kernel, ins, **e.consts)
    return {"rel_below": _rel(outs[e.out], e.reference(ins)) < inp["rel"]}
