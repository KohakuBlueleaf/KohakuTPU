"""A `.ktpu` kernel to what a card receives.

    text = l1_text(module, "softmax", "l3")         # the language's pipeline
    prog = program(read(text), "softmax", binds)    # L1 -> Program
    pkg, bindings = package(prog, fetch, resident)  # Program -> L0 bytes

`l1_text` runs the language's stages against `target.TARGET`; `package`
lowers for the node's dispatch engine, as the node runs it.
"""

from kohakuaccel.compiler.package import engine
from kohakutpu.compiler import target as T
from kohakutpu.compiler.emit import node
from kohakutpu.language import pipeline


def l1_text(
    module,
    name: str,
    level: str,
    l1=pipeline.DEFAULT_L1,
    l2=pipeline.DEFAULT_L2,
    options=None,
    target=T.TARGET,
) -> str:
    """`name`'s L1 text compiled from its body at `level` (`l2` or `l3`)."""
    return pipeline.compile_l1(module, name, level, target, l1, l2, options)


def program(module, name: str, binds: dict, machine=None, entry=None):
    """`fn name.l1` with each parameter bound to an address, as a `Program`
    for `machine` (the v9 die by default)."""
    kw = {} if entry is None else {"entry": entry}
    return node.program(module, name, binds, machine or T.machine(), **kw)


def package(prog, fetch=None, resident=None, addresses=None, spans=None) -> tuple:
    """``(Package, bindings)``: `prog` lowered for the node's dispatch engine,
    with no buffer defaults (it runs only at `bindings`). `fetch`, `resident`,
    `addresses`, `spans` are `Program.build`'s."""
    b = prog.build(fetch, resident, addresses, spans)
    return engine.lower(b.build(defaults=False)), b.bindings()


__all__ = ["l1_text", "package", "program"]
