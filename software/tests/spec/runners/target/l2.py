"""L2 card and Verilated execution through Rust, compared with numpy values."""

import os

import numpy as np
from harness.fresh import track
from harness.registry import runner

from kohakutpu.application.runtime.call import call
from kohakutpu.application.runtime.device import Device
from kohakutpu.language.examples.dsl.cluster.attention import attention
from kohakutpu.language.numerics.mxfp7 import value_fp16
from kohakutpu.language.text import module as M
from kohakutpu.language.text.syntax import TextError
from kohakutpu.stack import kernel, rs
from kohakutpu.stack.buffer import Buffer


def _ref(name, args):
    v = {k: np.asarray(x, np.float64) for k, x in args.items()}
    if name == "silu":
        return v["x"] / (1 + np.exp(-v["x"]))
    if name == "rows4":
        return v["x"][4:8]
    if name == "xlane1":
        return v["x"].reshape(-1, 16)[:, np.arange(16) ^ 1].reshape(v["x"].shape)
    if name == "xrot3":
        return v["x"].reshape(-1, 16)[:, (np.arange(16) + 3) % 16].reshape(v["x"].shape)
    if name == "softmax":
        e = np.exp(v["x"] - v["x"].max(1, keepdims=True))
        return e / e.sum(1, keepdims=True)
    if name == "gather":
        return v["t"][np.asarray(args["idx"], np.int64)[: len(v["y"])]]
    if name == "mxmm":
        return value_fp16(args["x"]) @ value_fp16(args["w"]).T
    raise ValueError(f"no reference {name}")


def _build(text, name, machine, passes=True):
    got = rs.compile(text, name, machine, passes=passes)
    if got["bound"] is not None and not kernel.BOUND_FALLBACK:
        raise TextError(f"order: STACK.RELOC: {got['bound']}", name=f"<{name}.l2>")
    return kernel.Compiled(
        name,
        text,
        M.read(got["l1"], f"<{name}.l1>"),
        machine,
        got["verdict"],
        got["packages"],
        None,
        bound=got["bound"],
        scratch=got.get("scratch"),
    )


def _run_l1(dev, c, name, inp):
    rng = np.random.default_rng(inp.get("seed", 1))
    args, binds, layout = {}, {}, {}
    try:
        for p, t in c.l1.body(name, "l1").params:
            b = Buffer.of(t, dev.card.machine_json)
            layout[p] = b
            x = None
            if p in inp.get("ints", {}):
                x = np.asarray(inp["ints"][p], b.host_dtype).reshape(b.host_shape)
            elif p in inp["inputs"]:
                x = (rng.standard_normal(b.host_shape) * inp.get("scale", 1.0)).astype(
                    b.host_dtype
                )
            if x is not None:
                args[p] = x
            binds[p] = dev.alloc(b.nbytes, b.space, b.layout["node"])
            dev.write(binds[p], bytes(b.nbytes) if x is None else b.pack(x))
        dev.run(c, binds)
        b = layout[inp["output"]]
        out = b.unpack(dev.read(binds[inp["output"]], b.nbytes))
    finally:
        for at in binds.values():
            dev.free(at)
    return args, out


def _on_device(dev, inp):
    text = (
        "\n".join(inp["text"]) if isinstance(inp["text"], list) else inp["text"]
    ) + "\n"
    name = inp.get("name") or M.read(text, "<spec>").names()[0]
    args, got = _run_l1(dev, _build(text, name, dev.machine), name, inp)
    if inp.get("against") == "nopasses":
        _, want = _run_l1(dev, _build(text, name, dev.machine, passes=False), name, inp)
        return {"match": bool(np.array_equal(got, want))}
    want = _ref(inp["ref"], {**args, "y": got})
    got = got.reshape(-1)[: want.size].reshape(want.shape)
    tol = inp.get("tol", 0)
    if tol == 0:
        return {"match": bool(np.array_equal(got, want))}
    err = float(np.abs(got - want).max() / max(np.abs(want).max(), 1e-30))
    return {"match": err <= tol}


@runner("l2.card")
def run_card(inp, ctx):
    return _on_device(ctx["session"].dev, inp)


@runner("l2.attention.card")
def run_attention_card(inp, ctx):
    """Dense attention on RTL against numpy f32 with MXFP7 operand rounding."""
    rng = np.random.default_rng(inp.get("seed", 5))
    s, t, d = inp["shape"]
    args = {
        name: rng.standard_normal(shape).astype(np.float16)
        for name, shape in (("q", (s, d)), ("k", (t, d)), ("vt", (d, t)))
    }
    out, _ = call(ctx["session"].dev, attention, args, tq=32, tk=64)
    q, k, vt = (value_fp16(args[n]).astype(np.float32) for n in ("q", "k", "vt"))
    scores = (q @ k.T) * np.float32(d**-0.5)
    p = np.exp(scores - scores.max(-1, keepdims=True))
    want = (value_fp16(p.astype(np.float16)).astype(np.float32) @ vt.T) / p.sum(
        -1, keepdims=True
    )
    got = np.asarray(out["o"], np.float32)
    error = np.max(np.abs(got - want)) / max(float(np.max(np.abs(want))), 1e-30)
    return {"match": bool(np.isfinite(got).all() and error <= inp["tol"])}


_VERILATED = {}


def verilated(inp, ctx):
    """The selected model, opened before poisoned or mutated execution."""
    nodes = inp.get("nodes", 1)
    board = os.environ.get("KTPU_BOARD", ctx["vector"]["machine"])
    key = (board, nodes, os.environ["KTPU_SPEC_VERILATOR"])
    if key not in _VERILATED:
        _VERILATED[key] = Device(kind="sim", build=key[2], nodes=nodes, board=key[0])
        track(_VERILATED[key].card)
    return _VERILATED[key]


@runner("l2.verilated")
def run_verilated(inp, ctx):
    return _on_device(verilated(inp, ctx), inp)
