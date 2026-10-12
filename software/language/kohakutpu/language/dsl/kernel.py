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

import inspect
import types
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast, overload

from kohakutpu.language.dsl.errors import DSLError
from kohakutpu.language.dsl.trace import Trace, recording
from kohakutpu.language.dsl.types import Machine, TensorSpec
from kohakutpu.language.dsl.values import Param


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

    def spec(self, name: str) -> TensorSpec:
        return dict(self.params)[name]


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
        `consts`. A `kt.auto` constant takes its first candidate whose trace
        succeeds and passes `check` (a function of the `Traced`; the compiler
        passes one that compiles it)."""
        given = dict(given or {})
        machine = machine or Machine()
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
                errors.append(f"{c}: {e}")
        raise DSLError(
            f"{self.name}: no kt.auto candidate traces and compiles:\n  "
            + "\n  ".join(errors)
        )

    def _trace(self, given, consts, machine) -> Traced:
        specs = self.specs(given, consts)
        key = (
            tuple(sorted(specs.items())),
            tuple(sorted(consts.items(), key=str)),
            machine,
        )
        hit = self._cache.get(key)
        if hit is not None:
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
        )
        self._cache[key] = traced
        return traced


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


__all__ = ["Auto", "In", "Kernel", "Out", "Port", "Traced", "auto", "kernel"]
