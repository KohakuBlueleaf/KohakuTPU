"""DSL kernel + tensors -> L2 -> L1: what a runtime calls to get a program.

    compiled = kernel.specialise(silu, {"x": x_spec}, machine=T.machine())
    compiled.l2_text, compiled.l1_text      # the levels, as text
    progs = compiled.programs(binds)        # {node: Program} for a card

`specialise` traces the kernel for the given tensors (a `kt.auto` constant
takes the first candidate that compiles), runs the L2 pipeline
(`l2.pipeline.compile_l1`: the L2 -> L2 passes, the lowering, the image
scheduler) and keeps both texts. Compilations are cached by L2 text and
machine: a second call with the same shapes compiles nothing.
"""

import os
import pathlib
from dataclasses import dataclass

from kohakutpu.compiler import build
from kohakutpu.compiler import target as T
from kohakutpu.compiler.l2 import pipeline
from kohakutpu.language.dsl import Machine, Traced
from kohakutpu.language.text import module as M

#: Set to a directory to keep every compiled kernel's L2 and L1 text there.
DUMP_ENV = "KTPU_DUMP"


@dataclass
class Compiled:
    """One kernel specialisation through the stack."""

    traced: Traced
    l1: M.Module
    machine: object

    @property
    def name(self) -> str:
        return self.traced.name

    @property
    def params(self) -> tuple:
        """``((name, TensorSpec), ...)``: every buffer the program binds."""
        return self.traced.params

    @property
    def l2_text(self) -> str:
        return self.traced.text

    @property
    def l1_text(self) -> str:
        return self.l1.source

    def programs(self, binds: dict) -> dict:
        """``{node: Program}`` with each parameter bound to an address
        (``{None: Program}`` for a one-node body)."""
        return build.programs(self.l1, self.name, binds, self.machine)


def dsl_machine(machine) -> Machine:
    """What a kernel may read about `machine` while tracing."""
    return Machine(
        mg=machine.count("MG"), vc=machine.count("VC"), nodes=len(machine.meshes)
    )


_cache: dict = {}


def compile_traced(traced: Traced, machine=None) -> Compiled:
    """`traced`'s L2 through the pipeline to L1. Raises `TextError` (the L2
    line) where the L2 compiler refuses it."""
    machine = machine or T.machine()
    key = (traced.text, repr(machine))  # a MachineSpec holds dicts: no hash
    got = _cache.get(key)
    if got is not None:
        return got
    module = M.read(traced.text, f"<{traced.name}.l2>")
    l1 = pipeline.compile_l1(module, traced.name, machine)
    if not isinstance(l1, M.Module):
        raise TypeError(
            f"{traced.name}: the L2 pipeline returned {type(l1).__name__}, not an L1 module"
        )
    got = Compiled(traced, l1, machine)
    _cache[key] = got
    _dump(got)
    return got


def specialise(kernel, given: dict, machine=None, **consts) -> Compiled:
    """Trace `kernel` for `given` (port -> tensor or spec) and compile it."""
    machine = machine or T.machine()
    found = {}

    def check(traced):
        found["c"] = compile_traced(traced, machine)

    traced = kernel.trace(given, machine=dsl_machine(machine), check=check, **consts)
    return found.get("c") or compile_traced(traced, machine)


def _dump(c: Compiled) -> None:
    where = os.environ.get(DUMP_ENV)
    if not where:
        return
    d = pathlib.Path(where)
    d.mkdir(parents=True, exist_ok=True)
    stem = f"{c.name}_{abs(hash(c.l2_text)) % 10**8:08d}"
    (d / f"{stem}.l2.ktpu").write_text(c.l2_text, encoding="utf-8")
    (d / f"{stem}.l1.ktpu").write_text(c.l1_text, encoding="utf-8")


__all__ = ["DUMP_ENV", "Compiled", "compile_traced", "dsl_machine", "specialise"]
