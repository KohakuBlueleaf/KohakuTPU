"""DSL kernel examples (`kohakutpu.language.dsl`), each with a sample
specialisation and a float64 numpy reference:

    from kohakutpu.language.examples import dsl as ex
    e = ex.EXAMPLES["attention"]
    print(e.kernel.trace(e.given, **e.consts).text)      # its L2 text
    inputs = e.inputs(np.random.default_rng(0)); want = e.reference(inputs)

`vector/` holds row kernels (activation, softmax, layernorm, rmsnorm, glu);
`cluster/` contractions with epilogues: linear, mlp, swiglu, attention
(shared memory: they run on every node's units unchanged) and splitk (a
cross-node connection); `acts` the activation table they pick from by name
at trace time.
"""

from dataclasses import dataclass, field

import numpy as np

from kohakutpu.language.dsl import Kernel
from kohakutpu.language.examples.dsl.acts import GELU_K
from kohakutpu.language.examples.dsl.cluster import (
    attention,
    linear,
    mlp,
    splitk,
    swiglu,
)
from kohakutpu.language.examples.dsl.vector import (
    activation,
    glu,
    layernorm,
    rmsnorm,
    softmax,
)
from kohakutpu.language.numerics.mxfp7 import value_fp16


@dataclass(frozen=True)
class Example:
    """A kernel, the shapes it is shown at, and what it should compute."""

    kernel: Kernel
    shapes: dict  # input port -> shape (f16)
    consts: dict = field(default_factory=dict)
    out: str = "y"
    #: bound on max |got - want| / max |want| on the card
    tol: float = 4e-3
    #: inputs drawn N(0, 1) / sqrt(last extent) (weights: unit-scale outputs)
    scaled: tuple = ()

    @property
    def given(self) -> dict:
        return {n: (s, "f16") for n, s in self.shapes.items()}

    def inputs(self, rng) -> dict:
        out = {}
        for n, s in self.shapes.items():
            a = rng.standard_normal(s)
            if n in self.scaled:
                a = a / np.sqrt(s[-1])
            if n == "g" and self.kernel.name in ("layernorm", "rmsnorm"):
                a = a * 0.5 + 1.0
            out[n] = a.astype(np.float16)
        return out

    def reference(self, ins: dict) -> np.ndarray:
        return REFERENCES[self.kernel.name](ins, self.consts)


def _f(a):
    return np.asarray(a, np.float64)


def _mx(a):
    """The values an MXFP7 operand holds (what the clusters multiply)."""
    return value_fp16(np.asarray(a, np.float16))


def _act(name, v):
    def sig(z):
        return 1.0 / (1.0 + np.exp(-z))

    return {
        "silu": lambda: v * sig(v),
        "gelu": lambda: v * sig(GELU_K * v),
        "relu": lambda: np.maximum(v, 0.0),
        "sigmoid": lambda: sig(v),
        "none": lambda: v,
    }[name]()


def _activation(i, c):
    v = _f(i["x"]) * c.get("scale", 1.0) + c.get("shift", 0.0)
    return _act(c.get("act", "silu"), v)


def _softmax(i, c):
    s = _f(i["x"]) / c.get("temperature", 1.0)
    e = np.exp(s - s.max(-1, keepdims=True))
    return e / e.sum(-1, keepdims=True)


def _layernorm(i, c):
    x = _f(i["x"])
    d = x - x.mean(-1, keepdims=True)
    inv = 1.0 / np.sqrt((d * d).mean(-1, keepdims=True) + c.get("eps", 1e-5))
    return d * inv * _f(i["g"]) + _f(i["b"])


def _rmsnorm(i, c):
    x = _f(i["x"])
    return (
        x / np.sqrt((x * x).mean(-1, keepdims=True) + c.get("eps", 1e-6)) * _f(i["g"])
    )


def _glu(i, c):
    return _f(i["a"]) * _act(c.get("act", "silu"), _f(i["g"]))


def _linear(i, c):
    return _act(c.get("act", "none"), _mx(i["x"]) @ _mx(i["w"]).T + _f(i["bias"]))


def _mlp(i, c):
    h = _act(c.get("act", "gelu"), _mx(i["x"]) @ _mx(i["w1"]).T + _f(i["b1"]))
    return _mx(h) @ _mx(i["w2"]).T + _f(i["b2"])


def _swiglu(i, c):
    x = _mx(i["x"])
    a = _act(c.get("act", "silu"), x @ _mx(i["w1"]).T) * (x @ _mx(i["w3"]).T)
    return _mx(a) @ _mx(i["w2"]).T


def _attention(i, c):
    h = c.get("heads", 1)
    q, k, vt = (np.split(_mx(i[n]), h) for n in ("q", "k", "vt"))
    scale = c.get("scale") or i["q"].shape[1] ** -0.5
    out = []
    for qh, kh, vh in zip(q, k, vt):
        s = qh @ kh.T * scale
        p = np.exp(s - s.max(-1, keepdims=True))
        out.append((_mx(p) @ vh.T) / p.sum(-1, keepdims=True))
    return np.concatenate(out)


def _splitk(i, c):
    return _mx(i["x"]) @ _mx(i["w"]).T


REFERENCES = {
    "activation": _activation,
    "softmax": _softmax,
    "layernorm": _layernorm,
    "rmsnorm": _rmsnorm,
    "glu": _glu,
    "linear": _linear,
    "mlp": _mlp,
    "swiglu": _swiglu,
    "attention": _attention,
    "splitk": _splitk,
}

EXAMPLES = {
    "activation": Example(activation, {"x": (256, 256)}, {"act": "gelu", "scale": 0.5}),
    "softmax": Example(softmax, {"x": (256, 128)}, {"temperature": 2.0}),
    "layernorm": Example(layernorm, {"x": (128, 256), "g": (256,), "b": (256,)}),
    "rmsnorm": Example(rmsnorm, {"x": (128, 192), "g": (192,)}),
    "glu": Example(glu, {"a": (128, 128), "g": (128, 128)}, {"act": "silu"}),
    "linear": Example(
        linear,
        {"x": (128, 256), "w": (512, 256), "bias": (512,)},
        {"act": "gelu"},
        tol=2e-2,
        scaled=("w",),
    ),
    "mlp": Example(
        mlp,
        {
            "x": (128, 256),
            "w1": (512, 256),
            "b1": (512,),
            "w2": (256, 512),
            "b2": (256,),
        },
        {"act": "gelu"},
        tol=3e-2,
        scaled=("w1", "w2"),
    ),
    "swiglu": Example(
        swiglu,
        {"x": (128, 256), "w1": (512, 256), "w3": (512, 256), "w2": (256, 512)},
        {"act": "silu"},
        tol=3e-2,
        scaled=("w1", "w3", "w2"),
    ),
    "attention": Example(
        attention,
        {"q": (128, 64), "k": (512, 64), "vt": (64, 512)},
        out="o",
        tol=3e-2,
    ),
    "heads": Example(
        attention,
        {"q": (256, 32), "k": (128, 32), "vt": (64, 64)},
        {"heads": 2},
        out="o",
        tol=3e-2,
    ),
    "splitk": Example(
        splitk, {"x": (64, 4096), "w": (512, 4096)}, tol=2e-2, scaled=("w",)
    ),
}

__all__ = [
    "EXAMPLES",
    "REFERENCES",
    "Example",
    "activation",
    "attention",
    "glu",
    "layernorm",
    "linear",
    "mlp",
    "rmsnorm",
    "softmax",
    "splitk",
    "swiglu",
]
