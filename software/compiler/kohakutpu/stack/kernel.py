"""Trace and compile kernels through Rust; retain relocatable run packages."""

import functools
import json
import os
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from pathlib import Path

from kohakutpu.application.runtime import packages as build
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.language import machine as MF
from kohakutpu.language.dsl import Machine, Traced
from kohakutpu.language.text import module as M
from kohakutpu.language.text.syntax import Call, Int, Stmt, TextError, emit, parse
from kohakutpu.stack import rs

DUMP_ENV = "KTPU_DUMP"
# Public compile-cache location reported by the DSL seam's frozen vectors.
CACHE_DIR = Path(__file__).resolve().parents[4] / "build" / "compile"
PROGRAMS_KEPT = 256
COMPILED_KEPT = 1024
_PROGS = OrderedDict()
_cache = OrderedDict()
BOUND_FALLBACK = True
BOUND_BUILDS = {}


@dataclass
class Compiled:
    name: str
    l2_text: str
    l1: M.Module
    machine: object
    verdict: dict | None = None
    packages: dict | None = None
    traced: Traced | None = None
    stack: str = "rust"
    level: str = "l2"
    bound: str | None = None
    scratch: list | None = None
    _progs: dict = field(default_factory=dict, repr=False, compare=False)
    _meta: tuple | None = field(default=None, init=False, repr=False, compare=False)

    @property
    def params(self):
        return self.traced.params

    @property
    def l1_text(self):
        return self.l1.source

    def programs(self, binds):
        key = tuple(sorted(binds.items()))
        got = self._progs.get(key)
        if got is None:
            got = self._prebuilt(binds)
            self._progs[key] = got
        _PROGS[(id(self), key)] = self
        _PROGS.move_to_end((id(self), key))
        while len(_PROGS) > PROGRAMS_KEPT:
            (_, old), owner = _PROGS.popitem(last=False)
            owner._progs.pop(old, None)
        return got

    def _prebuilt(self, binds):
        packages, verdict = self.packages, self.verdict
        apart = any(arena_name(a) not in binds for a in self.scratch or ())
        if self.bound is not None or apart:
            got = rs.compile(
                self.l1.source, self.name, self.machine, l1=True, binds=binds
            )
            packages, verdict = got["packages"], got["verdict"]
            if self.bound is not None:
                BOUND_BUILDS[self.name] = BOUND_BUILDS.get(self.name, 0) + 1
            found = tuple(
                build.Finding(f["kind"], f["at"], f["text"], f["clause"])
                for f in verdict["findings"]
            )
            text, fetch = rs.machine_json(self.machine), rs.fetch(self.machine)
        else:
            if self._meta is None:
                self._meta = (
                    tuple(
                        build.Finding(f["kind"], f["at"], f["text"], f["clause"])
                        for f in verdict["findings"]
                    ),
                    rs.machine_json(self.machine),
                    rs.fetch(self.machine),
                )
            found, text, fetch = self._meta
        out = {}
        for mesh, p in packages.items():
            at = [binds[q] if q in binds else int(q[1:], 16) for q in p["params"]]
            out[mesh] = build.Prebuilt(
                p["bytes"],
                tuple(at),
                text,
                mesh,
                found,
                verdict["refused"],
                fetch,
                images_json=p.get("images_json"),
                template_json=p.get("template_json"),
                template_params=p.get("template_params"),
            )
        return {None: out[0]} if list(out) == [0] else out


def arena_name(arena):
    node = arena.get("node")
    return f"scratch:{arena['space']}" + ("" if node is None else f":{node}")


@functools.lru_cache(maxsize=64)
def _dsl_machine_of(
    machine_json: str, nodes: int, mg: int, vc: int, sig_bits: int
) -> Machine:
    return replace(
        Machine.of(
            MF.Machine(json.loads(machine_json)["machine"]),
            nodes=nodes,
        ),
        mg=mg,
        vc=vc,
        sig_bits=sig_bits,
    )


def dsl_machine(machine):
    if not machine.board:
        raise ValueError("a trace needs a named board's machine file (RUL.011)")
    return _dsl_machine_of(
        machine.machine_json,
        len(machine.meshes),
        machine.count("MG"),
        machine.count("VC"),
        machine.params["VC"]["sig_bits"],
    )


def compile(source, name=None, machine=None, level="l2"):
    if level not in ("l1", "l2"):
        raise ValueError(f"level {level!r}: l1 or l2")
    machine = machine or machine_of(rs.BASE)
    traced = source if isinstance(source, Traced) else None
    module = source if isinstance(source, M.Module) else None
    text = traced.text if traced else module.source if module else source
    if name is None:
        name = traced.name if traced else (module or M.read(text)).names()[0]
    # Include the mutation and zero-sign contexts so a warm compile cannot hide either.
    key = (
        text,
        name,
        rs.machine_json(machine),
        level,
        rs._MUTANT.get(),
        rs._ZERO_SIGN.get(),
    )
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    rust = rs.compile(
        traced.graph_json if traced else text, name, machine, l1=level == "l1"
    )
    if rust["bound"] is not None and not BOUND_FALLBACK:
        raise TextError(
            f"order: STACK.RELOC: {rust['bound']}", name=f"<{name}.{level}>"
        )
    got = Compiled(
        name,
        "" if level == "l1" else text,
        module if level == "l1" and module else M.Text(rust["l1"], f"<{name}.l1>"),
        machine,
        rust["verdict"],
        rust["packages"],
        traced,
        level=level,
        bound=rust["bound"],
        scratch=rust.get("scratch"),
    )
    _cache[key] = got
    if len(_cache) > COMPILED_KEPT:
        _, gone = _cache.popitem(last=False)
        for k in [k for k, owner in _PROGS.items() if owner is gone]:
            del _PROGS[k]
    _dump(got)
    return got


def on_mesh(module, name, mesh):
    """Give an unscoped L1 body's execution node an explicit node block."""
    statements = parse(module.source, module.file)
    for st in statements:
        if (
            st.op == "fn"
            and isinstance(st.args[0], Call)
            and st.args[0].name == f"{name}.l1"
            and not any(child.op == "node" for child in st.body)
        ):
            st.body = [Stmt("node", [Int(mesh)], body=st.body)]
    return M.read(emit(statements), module.file)


def compile_traced(traced, machine=None):
    return compile(traced, machine=machine)


def specialise(kernel, given, machine=None, **consts):
    machine = machine or machine_of(rs.BASE)
    found = {}

    def check(traced):
        found["c"] = compile_traced(traced, machine)

    traced = kernel.trace(given, machine=dsl_machine(machine), check=check, **consts)
    return found.get("c") or compile_traced(traced, machine)


def _dump(c):
    where = os.environ.get(DUMP_ENV)
    if where:
        d = Path(where)
        d.mkdir(parents=True, exist_ok=True)
        stem = f"{c.name}_{abs(hash(c.l2_text)) % 10**8:08d}"
        (d / f"{stem}.l2.ktpu").write_text(c.l2_text, encoding="utf-8")
        (d / f"{stem}.l1.ktpu").write_text(c.l1_text, encoding="utf-8")
