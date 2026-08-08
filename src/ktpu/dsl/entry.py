"""Getting into and out of a trace: specs in, a `Graph` out, cached by signature.

Tracing runs at call time, which is when the real shape is in hand -- so this is
where dynamic shape is handled and where it stops. A caller may be fully
dynamic; what reaches the IR never is (docs/driver/ir.md s6.1). Caching by shape
is the first of the three strategies in docs/driver/dsl.md s4, and the only one
that needs machinery.

The cache key is `(function identity, input shapes, input dtypes, every
non-tensor argument)`. Non-tensor arguments are in it because they participate in
compile-time control flow and therefore change the graph: `n_heads=8` and
`n_heads=16` are two different programs. Getting the key wrong means silently
handing back a graph specialised on a different constant, which is why it is the
one thing docs/driver/dsl.md s5 asks for a test of by name.
"""

import functools
import inspect
from dataclasses import dataclass

import numpy as np

from ktpu.dsl.context import TraceContext
from ktpu.dsl.errors import DSLError
from ktpu.dsl.tracer import Tracer
from ktpu.ir import FP16, FP32, INT8, INT16, INT32, DType, Graph

#: Host buffers arrive as numpy arrays, so their dtypes have to map. Deliberately
#: short: E8M15 and ACC24 are machine formats and no numpy array holds one.
NUMPY_DTYPES = {
    np.float32: FP32,
    np.float16: FP16,
    np.int8: INT8,
    np.int16: INT16,
    np.int32: INT32,
}


@dataclass(frozen=True)
class TensorSpec:
    """What a kernel needs to know about an argument before it exists.

    Shape and dtype and nothing else -- no data. Tracing never reads a value, so
    a spec is enough, which is what makes `trace(f, spec, spec)` possible without
    allocating anything.
    """

    shape: tuple[int, ...]
    dtype: DType

    def __post_init__(self) -> None:
        object.__setattr__(self, "shape", tuple(int(d) for d in self.shape))

    def __str__(self) -> str:
        dims = "x".join(str(d) for d in self.shape) or "scalar"
        return f"{dims}:{self.dtype}"


def spec_of(obj) -> TensorSpec | None:
    """Is this argument a tensor, and if so what shape and dtype?

    Returns None for everything else, which is how an argument becomes part of
    the cache key instead of a graph input.
    """
    if isinstance(obj, TensorSpec):
        return obj
    if isinstance(obj, Tracer):
        # A Tracer answers both questions, so it would otherwise become a fresh
        # input of a fresh graph and detach from the trace it came from.
        raise DSLError(
            f"{obj} is a traced value and cannot be a kernel argument. To call "
            "one kernel from inside another, call the undecorated function -- "
            "`other.__wrapped__` -- so that it inlines into the graph being "
            "traced, which is what tracing is for."
        )
    if isinstance(obj, np.ndarray):
        dtype = NUMPY_DTYPES.get(obj.dtype.type)
        if dtype is None:
            raise DSLError(
                f"no machine dtype for numpy {obj.dtype}; the host formats are "
                f"{', '.join(sorted(d.name for d in NUMPY_DTYPES.values()))}"
            )
        return TensorSpec(obj.shape, dtype)
    # Anything else that can answer both questions -- a Value, a runtime buffer,
    # a tinygrad tensor once that bridge exists. Duck typing here rather than a
    # registry, because the two attributes ARE the interface.
    if hasattr(obj, "shape") and isinstance(getattr(obj, "dtype", None), DType):
        return TensorSpec(tuple(obj.shape), obj.dtype)
    return None


def trace(fn, *args, **kwargs) -> Graph:
    """Trace `fn` once, with no caching. The explicit entry point.

    Tensor arguments -- anywhere in the arguments, including inside lists, tuples
    and dicts -- become graph inputs in the order they are met. Everything else
    is passed through untouched and is ordinary Python inside the kernel, which
    is what makes compile-time control flow free.

    There is deliberately no `name=` keyword: every keyword here is forwarded to
    the kernel, so reserving one would shadow a parameter of that name. The graph
    takes the function's name, and `graph.name` is writable afterwards.
    """
    fn = inspect.unwrap(fn)
    bound = _bind(fn, args, kwargs)
    ctx = TraceContext(getattr(fn, "__name__", "graph"))

    def as_input(spec: TensorSpec, path: str) -> Tracer:
        return Tracer(ctx, ctx.emit(ctx.graph.input, spec.shape, spec.dtype, path))

    for param in list(bound.arguments):
        bound.arguments[param] = _map_tensors(bound.arguments[param], param, as_input)

    result = fn(*bound.args, **bound.kwargs)
    outputs = _outputs(result, ctx)
    ctx.graph.output(*outputs)
    # From here on the graph is finished. A tracer that escaped -- into a global,
    # a closure, a default argument -- would otherwise keep appending to it, and
    # the appended ops would be dead in a graph nobody thought was still open.
    ctx.closed = True
    return ctx.graph


class Kernel:
    """A traced function, plus one graph per distinct set of arguments.

    Calling it returns the `Graph`. When levels 2 and 3 land, `__call__` becomes
    "run it" and `.graph()` stays the way to ask for the graph -- so kernels that
    want the graph should say `.graph(...)` today and not have to change.
    """

    def __init__(self, fn) -> None:
        self._fn = fn
        self._cache: dict[tuple, Graph] = {}
        self._hits = 0
        self._misses = 0
        functools.update_wrapper(self, fn)

    def __call__(self, *args, **kwargs) -> Graph:
        return self.graph(*args, **kwargs)

    def graph(self, *args, **kwargs) -> Graph:
        """The traced graph for these arguments, from cache if it is there."""
        key = _cache_key(self._fn, args, kwargs)
        cached = self._cache.get(key)
        if cached is not None:
            self._hits += 1
            return cached
        self._misses += 1
        graph = trace(self._fn, *args, **kwargs)
        self._cache[key] = graph
        return graph

    def retrace(self, *args, **kwargs) -> Graph:
        """Trace again, ignoring and not filling the cache. For tests."""
        return trace(self._fn, *args, **kwargs)

    def cache_info(self) -> dict[str, int]:
        return {"hits": self._hits, "misses": self._misses, "size": len(self._cache)}

    def cache_clear(self) -> None:
        self._cache.clear()
        self._hits = self._misses = 0

    def __repr__(self) -> str:
        return f"<kernel {getattr(self._fn, '__name__', '?')} {self.cache_info()}>"


def kernel(fn) -> Kernel:
    """Decorator: trace on first call, cache by signature."""
    return Kernel(fn)


# ---- machinery -------------------------------------------------------------


def _bind(fn, args, kwargs) -> inspect.BoundArguments:
    """Name every argument, defaults included.

    Defaults matter as much as passed arguments: `eps=1e-6` is a constant folded
    into the graph, so a kernel called with it and one called without it are the
    same graph only because the default made them so. Binding is also what makes
    `f(x, alpha=1)` and `f(x, 1)` hit the same cache entry.
    """
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError) as exc:
        raise DSLError(
            f"cannot read the signature of {fn!r}; a kernel is an ordinary "
            "Python function"
        ) from exc
    bound = signature.bind(*args, **kwargs)
    bound.apply_defaults()
    return bound


def _map_tensors(obj, path: str, on_tensor):
    """Replace every tensor spec in a nested structure, leaving the rest alone."""
    spec = spec_of(obj)
    if spec is not None:
        return on_tensor(spec, path)
    match obj:
        case tuple():
            return tuple(
                _map_tensors(o, f"{path}[{i}]", on_tensor) for i, o in enumerate(obj)
            )
        case list():
            return [
                _map_tensors(o, f"{path}[{i}]", on_tensor) for i, o in enumerate(obj)
            ]
        case dict():
            return {
                k: _map_tensors(v, f"{path}[{k!r}]", on_tensor) for k, v in obj.items()
            }
        case _:
            return obj


def _cache_key(fn, args, kwargs) -> tuple:
    parts: list[tuple] = []
    for param, value in _bind(fn, args, kwargs).arguments.items():
        _key_of(value, param, parts)
    return tuple(parts)


def _key_of(obj, path: str, out: list[tuple]) -> None:
    spec = spec_of(obj)
    if spec is not None:
        out.append(("t", path, spec.shape, spec.dtype.name))
        return
    match obj:
        case tuple() | list():
            for i, o in enumerate(obj):
                _key_of(o, f"{path}[{i}]", out)
        case dict():
            for k, v in obj.items():
                _key_of(v, f"{path}[{k!r}]", out)
        case _:
            try:
                hash(obj)
            except TypeError as exc:
                raise DSLError(
                    f"argument {path}={obj!r} is not hashable, so it cannot go in "
                    "the cache key. Every non-tensor argument must, because it "
                    "may have steered compile-time control flow."
                ) from exc
            # The type name is in the key because hash(1) == hash(1.0) ==
            # hash(True) and all three compare equal: without it a graph
            # specialised on an int would be handed back for a float.
            out.append(("v", path, type(obj).__name__, obj))


def _outputs(result, ctx: TraceContext) -> tuple:
    """A kernel returns a traced value, or a tuple of them (docs/driver/dsl.md s7)."""
    values = result if isinstance(result, (tuple, list)) else (result,)
    out = []
    for i, item in enumerate(values):
        if not isinstance(item, Tracer):
            raise DSLError(
                f"a kernel returns traced values; output {i} is a "
                f"{type(item).__name__}. A kernel that computes nothing from its "
                "inputs has no graph."
            )
        if item._ctx is not ctx:
            raise DSLError(
                f"output {i} ({item}) was traced somewhere else -- it belongs to "
                "another call, so this graph does not contain the ops that "
                "produced it."
            )
        out.append(item.value)
    return tuple(out)
