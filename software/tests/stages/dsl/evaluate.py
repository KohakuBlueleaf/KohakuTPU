"""The DSL's meaning, on numpy: a traced graph run over logical tensors. It reads
the graph alone and shares nothing with the Rust path to L2, so an L2 that ends
elsewhere is that path's doing. Numbers are ideal; the bound is the caller's."""

import math
from typing import cast

import numpy as np


def index(e, env):
    if isinstance(e, int):
        return e
    return e["const"] + sum(atom(a, env) * c for a, c in e["terms"])


def atom(a, env):
    if "var" in a:
        return env[a["var"]]
    if "floor" in a:
        x, y = a["floor"]
        return index(x, env) // index(y, env)
    if "mod" in a:
        x, y = a["mod"]
        return index(x, env) % index(y, env)
    x, y = a["mul"]
    return index(x, env) * index(y, env)


def holds(c, env):
    a, b = index(c["a"], env), index(c["b"], env)
    return {
        "<": a < b,
        "<=": a <= b,
        ">": a > b,
        ">=": a >= b,
        "==": a == b,
        "!=": a != b,
    }[c["op"]]


def slices(axes, env):
    out = []
    for a in axes:
        if a == "all":
            out.append(slice(None))
        elif a == "new":
            out.append(None)
        elif "run" in a:
            lo, n = a["run"]
            lo = index(lo, env)
            out.append(slice(lo, lo + n))
        else:
            out.append(index(a["idx"], env))
    return tuple(out)


MATH = {
    "exp2": np.exp2,
    "log2": np.log2,
    "inv": lambda x: 1 / x,
    "rsqrt": lambda x: 1 / np.sqrt(x),
    "neg": np.negative,
    "abs": np.abs,
    "mov": np.copy,
    "add": np.add,
    "sub": np.subtract,
    "mul": np.multiply,
    "max": np.maximum,
    "min": np.minimum,
    "exp2d": lambda a, b: np.exp2(a - np.floor(b)),
    "fma": lambda a, b, c: a * b + c,
    "fnma": lambda a, b, c: c - a * b,
    "sel": lambda m, a, b: np.where(m != 0, a, b),
    "cmplt": lambda a, b: (a < b).astype(np.float64),
    "cmpgt": lambda a, b: (a > b).astype(np.float64),
    "cmpeq": lambda a, b: (a == b).astype(np.float64),
}


def bands(x, shape):
    """A row broadcast's band repeated down `shape`'s rows."""
    if isinstance(x, np.ndarray) and x.ndim == 2 and x.shape[0] < shape[0]:
        return np.tile(x, (shape[0] // x.shape[0], 1))
    return x


class Evaluator:
    def __init__(self, graph: dict, inputs: dict) -> None:
        self.buffers = {}
        for p in [*graph["ports"], *graph["scratch"]]:
            self.buffers[p["name"]] = np.zeros(p["spec"]["shape"], np.float64)
        for name, value in inputs.items():
            self.buffers[name][...] = value
        self.links: dict = {}
        self.env: dict = {}
        self.lanes = 16
        self.block(graph["body"], {})

    def block(self, stmts, values) -> None:
        for s in stmts:
            getattr(self, "on_" + s["kind"])(s, values)

    def on_par(self, s, values) -> None:
        unit = s["unit"]["kind"]
        for i in range(index(s["lo"], self.env), index(s["hi"], self.env)):
            self.env[s["var"]] = i
            at = s["unit"]["index"]
            place = i if at is None else index(at, self.env)
            if unit == "node":
                self.block(s["body"], {})
            else:
                self.unit = (unit, place)
                self.block(s["body"], {})
        del self.env[s["var"]]

    def on_for(self, s, values) -> None:
        for i in range(index(s["lo"], self.env), index(s["hi"], self.env)):
            self.env[s["var"]] = i
            self.block(s["body"], values)
        del self.env[s["var"]]

    def on_when(self, s, values) -> None:
        if holds(s["cond"], self.env):
            self.block(s["body"], values)

    def on_sticky(self, s, values) -> None:
        """Ideal numbers never overflowed an `exp2d`."""

    def view(self, v):
        return self.buffers[v["buffer"]][slices(v["axes"], self.env)]

    def operand(self, a, values):
        if "value" in a:
            return values[a["value"]]
        if "bcast" in a:
            return values[a["bcast"]][:, None]
        if "float" in a:
            x = a["float"]
            return (
                float(x) if not isinstance(x, str) else float(x.replace("inf", "inf"))
            )
        if "vview" in a:
            return values[a["vview"]["value"]][slices(a["vview"]["axes"], self.env)]
        return self.view(a["view"])

    def array(self, a, values) -> np.ndarray:
        """An operand no step gives as a float."""
        return cast(np.ndarray, self.operand(a, values))

    def on_value(self, s, values) -> None:
        op, shape = s["op"], tuple(s["type"]["shape"])
        args = s["args"]
        if op == "zero":
            out = np.zeros(shape)
        elif op in ("fill", "load"):
            out = self.array(args[0], values).copy()
            if op == "load" and out.size != math.prod(shape):
                out = np.broadcast_to(out.reshape(-1), shape).copy()
        elif op == "gemm":
            a, b = (self.array(x, values) for x in args[:2])
            acc = self.operand(args[2], values) if len(args) > 2 else 0.0
            out = acc + a @ b.T
        elif op == "recv":
            out = self.links[self.unit].pop(0)
        elif op in ("rmax", "rsum"):
            x = self.array(args[0], values)
            out = x.max(-1) if op == "rmax" else x.sum(-1)
        elif op in ("xlane", "xrot"):
            out = self.shuffled(op, args, values)
        else:
            out = self.pure(op, shape, args, values)
        values[s["target"]] = out

    def pure(self, op, shape, args, values):
        x = [self.operand(a, values) for a in args]
        if op in ("unpack", "pack"):
            return cast(np.ndarray, x[0]).copy()
        return np.broadcast_to(MATH[op](*[bands(v, shape) for v in x]), shape).copy()

    def shuffled(self, op, args, values):
        x = self.array(args[0], values)
        k = int(self.operand(args[1], values))
        lane = np.arange(x.shape[-1]) % self.lanes
        chunk = np.arange(x.shape[-1]) - lane
        to = lane ^ k if op == "xlane" else (lane + k) % self.lanes
        return x[..., chunk + to]

    def on_store(self, s, values) -> None:
        dst = self.view(s["dst"])
        src = values[s["src"]]
        dst[...] = src.reshape(dst.shape) if src.size == dst.size else src

    def on_drain(self, s, values) -> None:
        src = values[s["src"]]
        to = s["dst"]
        if "view" in to:
            self.view(to["view"])[...] = src
            return
        unit = to["unit"]
        key = (unit["kind"], index(unit["index"], self.env))
        self.links.setdefault(key, []).append(src)

    def on_send(self, s, values) -> None:
        key = (s["dst"]["kind"], index(s["dst"]["index"], self.env))
        self.links.setdefault(key, []).append(values[s["src"]])

    def on_mover(self, s, values) -> None:
        kw = dict(s["kw"])
        dst = self.view(s["dst"])
        if s["op"] == "fill":
            dst[...] = float(kw["value"]["int"])
            return
        src = self.view(s["src"])
        if "perm" in kw:
            src = src.transpose(kw["perm"]["perm"])
        if s["op"] in ("copy", "gt4", "quant"):
            dst[...] = src
            return
        raise NotImplementedError(f"mover {s['op']}")


def evaluate(graph: dict, inputs: dict) -> dict:
    """Every buffer of `graph` after its body runs on `inputs`."""
    return Evaluator(graph, inputs).buffers
