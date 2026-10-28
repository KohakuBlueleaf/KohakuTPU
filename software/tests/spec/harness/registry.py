"""Named spec runners and reversible mutants, with Rust-native stage routing."""

import functools
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager

from harness.scope import scoped
from kohakutpu.stack import rs

Runner = Callable[[dict, dict], object]
RUNNERS: dict[str, Runner] = {}
MUTANTS: dict[str, Callable[[], AbstractContextManager]] = {}
SHAPED: set[str] = set()


def runner(name: str, shaped: bool = False) -> Callable[[Runner], Runner]:
    def put(fn: Runner) -> Runner:
        if name in RUNNERS:
            raise ValueError(f"runner {name} registered twice")

        @functools.wraps(fn)
        def run(inp: dict, ctx: dict) -> object:
            with scoped():
                return fn(inp, ctx)

        RUNNERS[name] = run
        if shaped:
            SHAPED.add(name)
        return fn

    return put


def register_native():
    """Register Rust spec entry points and their native mutation scopes."""
    for name in rs.spec_runners():

        def native(inp, ctx, name=name):
            v = ctx["vector"]
            out = rs.spec_run(
                name, inp, v.get("machine"), vector_id=v.id, expect=v["expect"]
            )
            ctx["expect"] = out["expect"]
            return out["result"]

        runner(name)(native)
    for name in rs.mutants():
        if name not in MUTANTS:
            MUTANTS[name] = functools.partial(rs.mutant, name)


def mutant(name: str):
    def put(fn: Callable[[], Iterator[None]]):
        if name in MUTANTS:
            raise ValueError(f"mutant {name} registered twice")
        MUTANTS[name] = contextmanager(fn)
        return MUTANTS[name]

    return put


@contextmanager
def patched(obj, attr: str, value):
    keep = getattr(obj, attr)
    setattr(obj, attr, value)
    try:
        yield
    finally:
        setattr(obj, attr, keep)


__all__ = ["MUTANTS", "RUNNERS", "SHAPED", "mutant", "patched", "runner"]
