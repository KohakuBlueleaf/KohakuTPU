"""`@kt.kernel`: a Python function traced once per specialisation into an L2
`fn NAME.l2`.

    @kt.kernel
    def silu(x=kt.In("R", 128), y=kt.Out("R", 128), *, rows=16):
        ...

Port defaults (`In`, `Out`) are buffers, keyword-only parameters constants.
A trace binds the named extents from the tensors given, sizes the outputs,
and runs the body: Python control flow over constants and shapes leaves
nothing in the L2 text. A port's `dtype` / `layout` is what the kernel needs
its buffer to be; the caller converts its tensor first. The cache key holds
every spec, constant and the machine.
"""

import contextvars
import dataclasses
import inspect
import json
import math
import types
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast, overload

import numpy as np

from kohakutpu.language.dsl.errors import DSLError
from kohakutpu.language.dsl.trace import Trace, recording
from kohakutpu.language.dsl.types import Machine, TensorSpec
from kohakutpu.language.dsl.values import Param

#: Active mutation switch (shared with `kohakutpu.stack.rs._MUTANT`).
_MUTANT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "rs_mutant", default=None
)
#: Specialisations retained in `_TRACES` and per `Kernel._cache`.
TRACES_KEPT = 4096


class Port:
    """A buffer parameter: its role (``in``, ``out``), extents (names, ints
    or None for any), element type (None: the caller's), layout (a dict, or a
    function of the constants and extents) and space."""

    def __init__(self, role, dims, dtype, layout, space) -> None:
        self.role, self.dims = role, tuple(dims)
        self.dtype, self.layout, self.space = dtype, layout, space

    def __repr__(self) -> str:
        return f"{self.role.capitalize()}({', '.join(map(str, self.dims))}, dtype={self.dtype})"


# A port is a parameter DEFAULT, so a type checker types the parameter by it:
# `In` / `Out` are declared to return the `Param` the body receives.
def In(
    *dims: int | str | None, dtype: str = "f16", layout=None, space: str = "dram"
) -> Param:
    """An input buffer of extents `dims`."""
    return cast(Param, Port("in", dims, dtype, layout, space))


def Out(
    *dims: int | str | None, dtype: str = "f16", layout=None, space: str = "dram"
) -> Param:
    """An output buffer; its extents are names an input bound, or ints."""
    return cast(Param, Port("out", dims, dtype, layout, space))


class Auto:
    """A constant the compiler picks: the first candidate whose trace and
    compile succeed (`Kernel.trace`'s `check`)."""

    def __init__(self, *candidates) -> None:
        if not candidates:
            raise DSLError("kt.auto needs at least one candidate")
        self.candidates = candidates

    def __repr__(self) -> str:
        return f"auto{self.candidates}"


def auto[T](*candidates: T) -> T:
    """Typed as a candidate: the body receives the one chosen."""
    return cast(T, Auto(*candidates))


def causes(errors: list) -> str:
    """The distinct failures of `errors` (``(candidate, exception)``) with
    their counts, commonest first: ``12 candidates: 12 x ValueError: ...``
    (each cause its exception's class and first line)."""
    counts = Counter(
        f"{type(e).__name__}: {(str(e).splitlines() or [''])[0]}" for _, e in errors
    )
    each = "; ".join(f"{n} x {why}" for why, n in counts.most_common())
    return f"{len(errors)} candidates: {each}"


@dataclass
class Traced:
    """One specialisation: the L2 module text, each buffer's spec and role,
    the constants it was traced with."""

    name: str
    text: str
    params: tuple  # ((name, TensorSpec), ...)
    roles: dict  # name -> "in" | "out"
    consts: dict
    machine: Machine
    source: str = ""
    #: the same trace as graph data (`dsl.graph.module`), what ktpu-graph reads
    graph: dict = field(default_factory=dict)
    _graph_json: str | None = field(default=None, init=False, repr=False, compare=False)

    def spec(self, name: str) -> TensorSpec:
        return dict(self.params)[name]

    @property
    def graph_json(self) -> str:
        if self._graph_json is None:
            self._graph_json = json.dumps(self.graph)
        return self._graph_json


def _spec_of(x) -> TensorSpec:
    """A TensorSpec from a spec, a ``(shape, dtype)`` pair, a shape tuple or
    any object with `.shape` and a `.dtype` (numpy-like or one of ours)."""
    if isinstance(x, TensorSpec):
        return x
    if isinstance(x, tuple) and len(x) == 2 and isinstance(x[1], str):
        return TensorSpec.make(x[0], x[1])
    if isinstance(x, (tuple, list)):
        return TensorSpec.make(x)
    shape = getattr(x, "shape", None)
    if shape is None:
        raise DSLError(f"a buffer argument has a shape, got {type(x).__name__}")
    dt = str(getattr(x, "dtype", "f16"))
    dt = {"float16": "f16", "float32": "f32", "half": "f16", "float": "f32"}.get(dt, dt)
    spec = getattr(x, "spec", None)
    if isinstance(spec, TensorSpec):
        return spec
    return TensorSpec.make(shape, dt if dt in ("f16", "f32") else "f16")


class Kernel:
    """A traced kernel and its specialisations."""

    def __init__(self, fn, name: str | None = None) -> None:
        self.fn = fn
        self.name = name or fn.__name__
        sig = inspect.signature(fn)
        self.ports: dict = {}
        self.consts: dict = {}
        for p in sig.parameters.values():
            if isinstance(p.default, Port):
                if p.kind is p.KEYWORD_ONLY:
                    raise DSLError(f"{self.name}: buffer {p.name} is positional")
                self.ports[p.name] = p.default
            elif p.kind is p.KEYWORD_ONLY:
                if p.default is p.empty:
                    raise DSLError(f"{self.name}: constant {p.name} needs a default")
                self.consts[p.name] = p.default
            else:
                raise DSLError(
                    f"{self.name}: parameter {p.name} is a buffer (default kt.In / "
                    f"kt.Out) or a keyword-only constant"
                )
        self._cache: dict = {}
        try:
            self.source = inspect.getsource(fn)
        except (OSError, TypeError):
            self.source = ""

    @classmethod
    def build(cls, name: str, fn, ports: dict, consts: dict | None = None) -> "Kernel":
        """A kernel whose ports are given rather than read off a signature:
        `fn(**buffers, **consts)` is the body (a generated kernel)."""
        k = cls.__new__(cls)
        k.fn, k.name = fn, name
        k.ports, k.consts = dict(ports), dict(consts or {})
        k._cache = {}
        k.source = ""
        return k

    @property
    def inputs(self) -> list:
        return [n for n, p in self.ports.items() if p.role == "in"]

    @property
    def outputs(self) -> list:
        return [n for n, p in self.ports.items() if p.role == "out"]

    def __repr__(self) -> str:
        return f"<kt.kernel {self.name}({', '.join(self.ports)})>"

    # -------------------------------------------------------- specialise
    def specs(self, given: dict, consts: dict) -> dict:
        """Each port's TensorSpec from the given tensors: extents bound by the
        inputs, outputs sized from them, layouts from the constants."""
        dims: dict = {}
        out: dict = {}
        for name, port in self.ports.items():
            if name not in given:
                if port.role == "in":
                    raise DSLError(f"{self.name}: no tensor for input {name}")
                continue
            spec = _spec_of(given[name])
            if len(spec.shape) != len(port.dims):
                raise DSLError(
                    f"{self.name}: {name} is rank {len(spec.shape)}, the kernel "
                    f"reads rank {len(port.dims)} ({port.dims})"
                )
            for d, n in zip(port.dims, spec.shape):
                if isinstance(d, int) and d != n:
                    raise DSLError(
                        f"{self.name}: {name} has extent {n} where the kernel says {d}"
                    )
                if isinstance(d, str) and dims.setdefault(d, n) != n:
                    raise DSLError(
                        f"{self.name}: extent {d} is {dims[d]} and {n} ({name})"
                    )
            out[name] = spec
        ns = types.SimpleNamespace(**consts, **dims)
        final = {}
        for name, port in self.ports.items():
            if name in out:
                shape = out[name].shape
                dtype = port.dtype or out[name].dtype
            else:
                try:
                    shape = tuple(
                        d if isinstance(d, int) else dims[d] for d in port.dims
                    )
                except KeyError as e:
                    raise DSLError(
                        f"{self.name}: output {name}'s extent {e.args[0]} is bound by no input"
                    ) from None
                dtype = port.dtype or "f16"
            layout = port.layout(ns) if callable(port.layout) else (port.layout or {})
            final[name] = TensorSpec.make(shape, dtype, layout, port.space)
        return final

    def trace(
        self, given: dict | None = None, *, machine=None, check=None, **consts
    ) -> Traced:
        """The specialisation for `given` (port name -> tensor or spec) and
        `consts`, requiring `machine=kt.Machine.of(machine_file)`. A `kt.auto`
        constant takes its first candidate whose trace
        succeeds and passes `check` (a function of the `Traced`; the compiler
        passes one that compiles it)."""
        given = dict(given or {})
        if not isinstance(machine, Machine):
            raise DSLError(
                f"{self.name}: trace needs the machine it is traced for "
                f"(machine=kt.Machine.of(<machine file>)), got {machine!r}"
            )
        unknown = set(consts) - set(self.consts)
        if unknown:
            raise DSLError(f"{self.name} has no constant {sorted(unknown)}")
        merged = {**self.consts, **consts}
        autos = [k for k, v in merged.items() if isinstance(v, Auto)]
        choices = [{}]
        for k in autos:
            choices = [{**c, k: v} for c in choices for v in merged[k].candidates]
        errors = []
        for c in choices:
            try:
                got = self._trace(given, {**merged, **c}, machine)
                if check is not None:
                    check(got)
                return got
            except (DSLError, ValueError) as e:
                if not autos:
                    raise
                errors.append((c, e))
        raise DSLError(
            f"{self.name}: no kt.auto candidate traces and compiles "
            f"({causes(errors)}):\n  " + "\n  ".join(f"{c}: {e}" for c, e in errors)
        )

    def graph(
        self, given: dict | None = None, *, machine=None, check=None, **consts
    ) -> dict:
        """The specialisation's graph data (`Traced.graph`), as `trace`
        picks it."""
        return self.trace(given, machine=machine, check=check, **consts).graph

    def _identity(self):
        """What decides this kernel's body, where each piece is a plain value:
        its function's code, the values its closure and defaults hold, its
        name and port roles. None where one is not (an object's repr may name
        an address another object later takes)."""
        fn = self.fn
        code = getattr(fn, "__code__", None)
        if code is None:
            return None
        cells = tuple(c.cell_contents for c in (fn.__closure__ or ()))
        defaults = (fn.__defaults__, tuple(sorted((fn.__kwdefaults__ or {}).items())))
        if not _plain((cells, defaults)):
            return None
        roles = tuple((n, p.role) for n, p in self.ports.items())
        return (code, self.name, cells, defaults, roles, Trace)

    def _trace(self, given, consts, machine) -> Traced:
        specs = self.specs(given, consts)
        ckey = tuple((k, _const_key(v)) for k, v in sorted(consts.items(), key=str))
        key = (
            tuple(sorted(specs.items())),
            ckey,
            machine,
            _MUTANT.get(),
        )
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        # The same body built again (a kernel factory called per launch) is
        # traced once.
        ident = self._identity()
        raw_consts = tuple(sorted(consts.items(), key=str))
        shared = None if ident is None or not _plain(raw_consts) else (ident, key)
        hit = _TRACES.get(shared) if shared is not None else None
        if hit is not None:
            self._cache[key] = hit
            while len(self._cache) > TRACES_KEPT:
                self._cache.pop(next(iter(self._cache)))
            return hit
        t = Trace(self.name, machine)
        params = {}
        for name, spec in specs.items():
            t.claim(name)
            t.params.append((name, spec))
            params[name] = Param(name, spec)
        with recording(t):
            self.fn(**params, **consts)
        if not t.root:
            raise DSLError(f"{self.name} recorded no statement")
        roles = {n: p.role for n, p in self.ports.items()}
        traced = Traced(
            self.name,
            t.text(),
            tuple(specs.items()),
            roles,
            dict(consts),
            machine,
            self.source,
            t.graph(roles, consts),
        )
        self._cache[key] = traced
        while len(self._cache) > TRACES_KEPT:
            self._cache.pop(next(iter(self._cache)))
        if shared is not None:
            _TRACES[shared] = traced
            while len(_TRACES) > TRACES_KEPT:
                _TRACES.pop(next(iter(_TRACES)))
        return traced


#: Traces by kernel identity (`Kernel._identity`) and specialisation.
_TRACES: dict = {}
_PLAIN = (int, float, str, bool, bytes, type(None))


def _const_key(v) -> tuple:
    """Exact structural key; array keys retain dtype, shape and full value bytes."""
    if v is None:
        return ("none",)
    if isinstance(v, bool):
        return ("bool", v)
    if isinstance(v, int):
        return ("int", int(v))
    if isinstance(v, float):
        if math.isnan(v):
            return ("float", "nan")
        return ("float", math.copysign(1.0, v), v)
    if isinstance(v, str):
        return ("str", v)
    if isinstance(v, bytes):
        return ("bytes", v)
    if isinstance(v, np.ndarray):
        return ("ndarray", v.shape, str(v.dtype), v.tobytes())
    if isinstance(v, np.generic):
        return ("np", str(v.dtype), v.tobytes())
    if isinstance(v, tuple):
        return ("tuple", tuple(_const_key(x) for x in v))
    if isinstance(v, list):
        return ("list", tuple(_const_key(x) for x in v))
    if isinstance(v, dict):
        return (
            "dict",
            tuple(
                sorted(
                    ((_const_key(k), _const_key(val)) for k, val in v.items()),
                    key=repr,
                )
            ),
        )
    if isinstance(v, (set, frozenset)):
        return ("set", frozenset(_const_key(x) for x in v))
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return (
            "dc",
            type(v),
            tuple(
                (f.name, _const_key(getattr(v, f.name))) for f in dataclasses.fields(v)
            ),
        )
    return ("obj", type(v), id(v))


def _plain(v) -> bool:
    """Whether `v` is built of plain values alone (numbers, strings, None,
    and tuples / frozensets / frozen dataclasses of them), so equal keys
    mean equal values."""
    if isinstance(v, _PLAIN):
        return True
    if isinstance(v, (tuple, frozenset)):
        return all(_plain(x) for x in v)
    if (
        dataclasses.is_dataclass(v)
        and not isinstance(v, type)
        and getattr(getattr(v, "__dataclass_params__", None), "frozen", False)
    ):
        return all(_plain(getattr(v, f.name)) for f in dataclasses.fields(v))
    return False


@overload
def kernel(fn: Callable[..., Any], *, name: str | None = None) -> Kernel: ...
@overload
def kernel(
    fn: None = None, *, name: str | None = None
) -> Callable[[Callable[..., Any]], Kernel]: ...
def kernel(
    fn: Callable[..., Any] | None = None, *, name: str | None = None
) -> Kernel | Callable[[Callable[..., Any]], Kernel]:
    """Decorate a kernel function (``@kt.kernel`` or ``@kt.kernel(name=...)``)."""
    if fn is None:
        return lambda f: Kernel(f, name)
    return Kernel(fn, name)


__all__ = ["Auto", "In", "Kernel", "Out", "Port", "Traced", "auto", "causes", "kernel"]
