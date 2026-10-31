"""Activations as DSL value arithmetic, picked by name at trace time: a
kernel's ``act="gelu"`` is a Python dict lookup, so each choice traces to
its own straight-line vector code and none costs a runtime branch."""

import kohakutpu.language.dsl as kt

#: gelu through the sigmoid form: x * sigmoid(1.702 x).
GELU_K = 1.702


def silu(v):
    return v * kt.recip(1.0 + kt.exp2(v * -kt.LOG2E))


def gelu(v):
    return v * kt.recip(1.0 + kt.exp2(v * (-GELU_K * kt.LOG2E)))


def relu(v):
    return kt.maximum(v, 0.0)


def sigmoid(v):
    return kt.recip(1.0 + kt.exp2(v * -kt.LOG2E))


def identity(v):
    return v


ACTS = {"silu": silu, "gelu": gelu, "relu": relu, "sigmoid": sigmoid, "none": identity}


def act(name: str):
    """The activation `name`; refused by name at trace time."""
    kt.require(name in ACTS, f"no activation {name!r}: {', '.join(ACTS)}")
    return ACTS[name]


__all__ = ["ACTS", "GELU_K", "act", "gelu", "identity", "relu", "sigmoid", "silu"]
