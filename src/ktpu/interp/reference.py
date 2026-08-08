"""Level-1 reference simulator: execute a Graph in float64 numpy.

Defines what "correct" means for every level below, so it is written the slow
obvious way and is never optimised. No dtypes, no tiles, no engines, no cost.
"""

import numpy as np

from ktpu.ir.graph import Graph, OpKind, Value

_UNARY = {
    OpKind.NEG: lambda a: -a,
    OpKind.ABS: np.abs,
    OpKind.RECIP: lambda a: 1.0 / a,
    OpKind.RSQRT: lambda a: 1.0 / np.sqrt(a),
    OpKind.SQRT: np.sqrt,
    OpKind.EXP2: np.exp2,
    OpKind.LOG2: np.log2,
    OpKind.RELU: lambda a: np.maximum(a, 0.0),
}

_BINARY = {
    OpKind.ADD: np.add,
    OpKind.SUB: np.subtract,
    OpKind.MUL: np.multiply,
    OpKind.DIV: np.divide,
    OpKind.MAX: np.maximum,
    OpKind.MIN: np.minimum,
    OpKind.CMPLT: lambda a, b: (a < b).astype(np.float64),
    OpKind.CMPGT: lambda a, b: (a > b).astype(np.float64),
    OpKind.CMPEQ: lambda a, b: (a == b).astype(np.float64),
}

_REDUCE = {
    OpKind.SUM: lambda a, ax: np.sum(a, axis=ax),
    OpKind.RMAX: lambda a, ax: np.max(a, axis=ax),
    OpKind.RMIN: lambda a, ax: np.min(a, axis=ax),
    OpKind.SUMSQ: lambda a, ax: np.sum(a * a, axis=ax),
}


class InterpError(RuntimeError):
    """A graph that cannot be executed as given."""


def run(graph: Graph, inputs: dict[str, np.ndarray]) -> list[np.ndarray]:
    """Execute `graph` and return its outputs as float64 arrays.

    `inputs` maps an input's `name` attribute to an array, which must match that
    input's shape. Unnamed inputs are looked up by position as "in0", "in1", ...

    Raises InterpError on a missing input, a shape mismatch, or an op with no
    reference implementation.
    """
    env: dict[int, np.ndarray] = {}
    pos = 0

    for op in graph.ops:
        match op.kind:
            case OpKind.INPUT:
                name = op.attrs.get("name") or f"in{pos}"
                pos += 1
                if name not in inputs:
                    raise InterpError(f"missing input {name!r}; have {sorted(inputs)}")
                arr = np.asarray(inputs[name], dtype=np.float64)
                if arr.shape != op.out.shape:
                    raise InterpError(
                        f"input {name!r} is {arr.shape}, graph says {op.out.shape}"
                    )
                env[op.out.vid] = arr

            case OpKind.CONST:
                env[op.out.vid] = np.float64(op.attrs["value"])

            case OpKind.FMA:
                a, b, c = (env[v.vid] for v in op.inputs)
                env[op.out.vid] = a * b + c

            case OpKind.SELECT:
                cond, t, f = (env[v.vid] for v in op.inputs)
                env[op.out.vid] = np.where(cond != 0.0, t, f)

            case OpKind.MATMUL:
                a, b = (env[v.vid] for v in op.inputs)
                env[op.out.vid] = a @ b

            case OpKind.CAST:
                env[op.out.vid] = env[op.inputs[0].vid]

            case OpKind.RESHAPE:
                env[op.out.vid] = env[op.inputs[0].vid].reshape(op.out.shape)

            case OpKind.PERMUTE:
                env[op.out.vid] = np.transpose(env[op.inputs[0].vid], op.attrs["order"])

            case OpKind.EXPAND:
                env[op.out.vid] = np.broadcast_to(
                    env[op.inputs[0].vid], op.out.shape
                ).copy()

            case OpKind.SLICE:
                sl = tuple(
                    slice(b, e) for b, e in zip(op.attrs["begin"], op.attrs["end"])
                )
                env[op.out.vid] = env[op.inputs[0].vid][sl].copy()

            case OpKind.CONCAT:
                env[op.out.vid] = np.concatenate(
                    [env[v.vid] for v in op.inputs], axis=op.attrs["axis"]
                )

            case OpKind.PAD:
                env[op.out.vid] = np.pad(
                    env[op.inputs[0].vid],
                    op.attrs["pads"],
                    constant_values=op.attrs.get("value", 0.0),
                )

            case _ if op.kind in _UNARY:
                env[op.out.vid] = _UNARY[op.kind](env[op.inputs[0].vid])

            case _ if op.kind in _BINARY:
                a, b = (env[v.vid] for v in op.inputs)
                env[op.out.vid] = _BINARY[op.kind](a, b)

            case _ if op.kind in _REDUCE:
                ax = tuple(op.attrs["axes"])
                out = _REDUCE[op.kind](env[op.inputs[0].vid], ax)
                if op.attrs.get("keepdim"):
                    out = np.expand_dims(out, ax)
                env[op.out.vid] = out

            case _:
                raise InterpError(f"no reference implementation for {op.kind.value}")

    return [np.asarray(env[v.vid]) for v in graph.outputs]


def run_one(graph: Graph, *arrays: np.ndarray) -> np.ndarray:
    """Execute a single-output graph with inputs given positionally.

    Convenience over `run` for the common case. Raises InterpError if the graph
    does not have exactly one output.
    """
    if len(graph.outputs) != 1:
        raise InterpError(f"graph has {len(graph.outputs)} outputs, expected 1")
    named: dict[str, np.ndarray] = {}
    for pos, v in enumerate(graph.inputs):
        name = graph.producer(v).attrs.get("name") or f"in{pos}"
        named[name] = arrays[pos]
    return run(graph, named)[0]


def shapes(graph: Graph) -> dict[int, tuple[int, ...]]:
    """Map every value id in `graph` to its shape. Useful for debugging a pass."""
    out: dict[int, tuple[int, ...]] = {}
    for op in graph.ops:
        out[op.out.vid] = op.out.shape
    return out


def value_of(graph: Graph, v: Value, inputs: dict[str, np.ndarray]) -> np.ndarray:
    """Execute `graph` far enough to produce `v`, and return it.

    Runs the whole graph and picks the value out; correctness over speed, in
    keeping with this module.
    """
    saved = list(graph.outputs)
    graph.outputs = [v]
    try:
        return run(graph, inputs)[0]
    finally:
        graph.outputs = saved
