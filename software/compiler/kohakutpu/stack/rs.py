"""Machine-bound Rust compiler and binding seam."""

import contextlib
import contextvars
import functools
import json
import os
import time

from kohakutpu.compiler import _rs
from kohakutpu.language import machine as MF
from kohakutpu.language.dsl.kernel import _MUTANT
from kohakutpu.language.text.syntax import TextError

MACHINE_ENV = "KTPU_RS_MACHINE"
BASE = "multimesh_v9t6"
_COMPILER_OWNER = contextvars.ContextVar("compiler_owner", default=None)
_ZERO_SIGN = contextvars.ContextVar("zero_sign", default=None)


@contextlib.contextmanager
def compiler_owner(device):
    token = _COMPILER_OWNER.set(device)
    try:
        yield
    finally:
        _COMPILER_OWNER.reset(token)


def machine_json(spec):
    named = os.environ.get(MACHINE_ENV)
    if named:
        return (MF.MACHINES / f"{named}.json").read_text(encoding="utf-8")
    if isinstance(spec, str):
        return (
            spec
            if spec.lstrip().startswith("{")
            else (MF.MACHINES / f"{spec}.json").read_text(encoding="utf-8")
        )
    return spec.machine_json


@functools.lru_cache(maxsize=64)
def _fetch_of(text: str):
    port = json.loads(text)["machine"]["die"].get("fetch_port")
    return None if port is None else tuple(port)


def fetch(spec):
    return _fetch_of(machine_json(spec))


def mutants():
    return list(_rs.MUTANTS)


@contextlib.contextmanager
def mutant(name):
    token = _MUTANT.set(name)
    try:
        yield
    finally:
        _MUTANT.reset(token)


@contextlib.contextmanager
def zero_sign(value):
    """Compiles and library calls inside lower with zero-sign handling `value`
    (one of `_rs.ZERO_SIGNS`; None: `_rs.ZERO_SIGN`)."""
    token = _ZERO_SIGN.set(value)
    try:
        yield
    finally:
        _ZERO_SIGN.reset(token)


def compile(source, name, spec, l1=False, binds=None, passes=True):
    owner = _COMPILER_OWNER.get()
    if owner is not None:
        owner.counters["compile_calls"] += 1
        if owner.library_only:
            raise RuntimeError(f"library_only forbids compiler call for {name}")
    started = time.perf_counter()
    try:
        return _rs.compile(
            source,
            machine_json(spec),
            name,
            fetch=True,
            mutant=_MUTANT.get(),
            l1=l1,
            binds=None if binds is None else dict(binds),
            passes=passes,
            zero_sign=_ZERO_SIGN.get(),
        )
    except _rs.Refused as exc:
        raise TextError(str(exc), name=f"<{name}.l2>") from exc
    finally:
        if owner is not None:
            owner.counters["compile_seconds"] += time.perf_counter() - started


def library_key(source, machine):
    return _rs.library_key(source, machine, zero_sign=_ZERO_SIGN.get())


def library_machine(machine):
    return _rs.library_machine(machine, zero_sign=_ZERO_SIGN.get())


def library_get(root, source, machine, mutant=None):
    return _rs.library_get(
        str(root), source, machine, mutant, zero_sign=_ZERO_SIGN.get()
    )


def library_prebuild(root, source, machine):
    return _rs.library_prebuild(str(root), source, machine, zero_sign=_ZERO_SIGN.get())


def template_compile(representative, sources, machine, parameter_rotations=False):
    return _rs.template_compile(
        representative,
        sources,
        machine,
        parameter_rotations,
        zero_sign=_ZERO_SIGN.get(),
    )


def bind(
    package,
    bindings,
    machine,
    mesh,
    lower,
    after=None,
    translations=None,
    cores=None,
    images_json=None,
    template_json=None,
    params=None,
):
    return _rs.bind(
        package,
        list(bindings),
        machine,
        mesh,
        lower,
        _MUTANT.get(),
        after,
        translations=translations,
        cores=cores,
        images_json=images_json,
        template_json=template_json,
        params=params,
    )


def spec_run(runner, inp, machine, *, vector_id=None, expect=None):
    return json.loads(
        _rs.spec_run(
            runner,
            json.dumps(inp),
            machine,
            _MUTANT.get(),
            vector_id,
            json.dumps(expect),
        )
    )


def spec_runners():
    return tuple(_rs.SPEC_RUNNERS)
