"""An L1 `.ktpu` body to what a card receives.

    prog = program(module, "softmax", binds)        # L1 -> Program
    pkg, bindings = package(prog, fetch, resident)  # Program -> L0 bytes

`package` lowers for the node's dispatch engine, as the node runs it.
"""

from kohakuaccel.compiler.package import engine
from kohakutpu.compiler import target as T
from kohakutpu.compiler.emit import node


def program(module, name: str, binds: dict, machine=None, entry=None):
    """`fn name.l1` with each parameter bound to an address, as a `Program`
    for `machine` (the v9 die by default)."""
    kw = {} if entry is None else {"entry": entry}
    return node.program(module, name, binds, machine or T.machine(), **kw)


def programs(module, name: str, binds: dict, machine, entry=None) -> dict:
    """``{node: Program}`` of a body written in `node N` blocks (``{None:
    Program}`` for one written without)."""
    kw = {} if entry is None else {"entry": entry}
    return node.programs(module, name, binds, machine, **kw)


def package(prog, fetch=None, resident=None, addresses=None, spans=None) -> tuple:
    """``(Package, bindings)``: `prog` lowered for the node's dispatch engine,
    with no buffer defaults (it runs only at `bindings`). `fetch`, `resident`,
    `addresses`, `spans` are `Program.build`'s."""
    b = prog.build(fetch, resident, addresses, spans)
    return engine.lower(b.build(defaults=False)), b.bindings()


__all__ = ["package", "program", "programs"]
