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

import functools
import hashlib
import os
import pathlib
import pickle
from collections import OrderedDict
from dataclasses import dataclass, field

import kohakuaccel
from kohakutpu import language
from kohakutpu.compiler import build
from kohakutpu.compiler import target as T
from kohakutpu.compiler.l2 import pipeline
from kohakutpu.language.dsl import Machine, Traced
from kohakutpu.language.text import module as M

#: Set to a directory to keep every compiled kernel's L2 and L1 text there.
DUMP_ENV = "KTPU_DUMP"
#: Compiled L1 modules on disk (`_stored`); the variable names another
#: directory, empty turns the store off.
CACHE_ENV = "KTPU_COMPILE_CACHE"
CACHE_DIR = pathlib.Path(__file__).resolve().parents[5] / "build" / "compile"
#: Built programs kept, by kernel and binding, over every kernel (fresh
#: scratch addresses bind anew; a convolution net's step is hundreds).
PROGRAMS_KEPT = 256
#: Compiled kernels kept (each holds its L1 module: ~3 MB for a convolution's
#: product, measured; a convolution net's step compiles a few hundred).
COMPILED_KEPT = 1024
#: ``{(id(compiled), binding): compiled}``, least recently used first.
_PROGS: OrderedDict = OrderedDict()


@dataclass
class Compiled:
    """One kernel specialisation through the stack."""

    traced: Traced
    l1: M.Module
    machine: object
    #: `programs` by binding (the most recently used PROGRAMS_KEPT over
    #: every kernel).
    _progs: dict = field(default_factory=dict, repr=False, compare=False)

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
        (``{None: Program}`` for a one-node body). A binding seen before
        returns the same programs (a `Program` is not changed by building)."""
        key = tuple(sorted(binds.items()))
        got = self._progs.get(key)
        if got is None:
            got = build.programs(self.l1, self.name, binds, self.machine)
            self._progs[key] = got
        _PROGS[(id(self), key)] = self
        _PROGS.move_to_end((id(self), key))
        while len(_PROGS) > PROGRAMS_KEPT:
            (_, old), owner = _PROGS.popitem(last=False)
            owner._progs.pop(old, None)
        return got


def dsl_machine(machine) -> Machine:
    """What a kernel may read about `machine` while tracing."""
    return Machine(
        mg=machine.count("MG"), vc=machine.count("VC"), nodes=len(machine.meshes)
    )


_cache: OrderedDict = OrderedDict()


def compile_traced(traced: Traced, machine=None) -> Compiled:
    """`traced`'s L2 through the pipeline to L1. Raises `TextError` (the L2
    line) where the L2 compiler refuses it."""
    machine = machine or T.machine()
    key = (traced.text, repr(machine))  # a MachineSpec holds dicts: no hash
    got = _cache.get(key)
    if got is not None:
        _cache.move_to_end(key)
        return got
    l1 = _stored(key)
    if l1 is None:
        module = M.read(traced.text, f"<{traced.name}.l2>")
        l1 = pipeline.compile_l1(module, traced.name, machine)
        if not isinstance(l1, M.Module):
            raise TypeError(
                f"{traced.name}: the L2 pipeline returned {type(l1).__name__}, "
                "not an L1 module"
            )
        _store(key, l1)
    got = Compiled(traced, l1, machine)
    _cache[key] = got
    if len(_cache) > COMPILED_KEPT:
        _, gone = _cache.popitem(last=False)
        for k in [k for k, owner in _PROGS.items() if owner is gone]:
            del _PROGS[k]
    _dump(got)
    return got


@functools.cache
def _sources() -> bytes:
    """A digest of every source the L2 -> L1 compilation can read (the
    kohakutpu compiler and language packages, the kohakuaccel framework): a
    stored module is used only by the code that made it."""
    tops = [pathlib.Path(__file__).resolve().parents[1]]
    tops += [pathlib.Path(p).resolve() for p in language.__path__]
    tops += [pathlib.Path(p).resolve() for p in kohakuaccel.__path__]
    h = hashlib.sha256()
    for top in tops:
        for p in sorted(top.rglob("*.py")):
            h.update(p.as_posix().encode() + p.read_bytes())
    return h.digest()


def _path(key: tuple) -> pathlib.Path | None:
    where = os.environ.get(CACHE_ENV, str(CACHE_DIR))
    if not where:
        return None
    h = hashlib.sha256(_sources())
    h.update("\0".join(key).encode())
    return pathlib.Path(where) / f"{h.hexdigest()[:40]}.l1"


def _stored(key: tuple) -> M.Module | None:
    """The L1 module a run of this same code compiled for `key`, or None."""
    path = _path(key)
    if path is None or not path.exists():
        return None
    return pickle.loads(path.read_bytes())


def _store(key: tuple, l1: M.Module) -> None:
    path = _path(key)
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_bytes(pickle.dumps(l1))
    tmp.replace(path)


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
