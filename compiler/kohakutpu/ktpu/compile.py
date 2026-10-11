"""The `.ktpu` pass pipeline: L3 -> L2 -> L2 passes -> L1 -> L1 passes, each
stage's output a `.ktpu` text read back by the next stage's reader.

A pass is named `NAME` or `NAME=ARG` (`retile=8`)."""

import copy

from kohakuaccel.text.module import read as read_module
from kohakutpu.ir.l1.model import machine
from kohakutpu.ktpu.l1 import schedule
from kohakutpu.ktpu.l2 import lower as l2_lower
from kohakutpu.ktpu.l2 import passes as l2_passes_mod
from kohakutpu.ktpu.l2 import reader as l2_reader
from kohakutpu.ktpu.l2 import verify as l2_verify
from kohakutpu.ktpu.l2.text import body_text
from kohakutpu.ktpu.l3.plan import plan_l2
from kohakutpu.ktpu.l3.reference import l3_module

#: Units a body may place on, counted from the L1 machine.
UNITS = {kind: len(at) for kind, at in machine().units.items()}
#: L1 -> L1 passes, text to text.
L1_PASSES = {
    "schedule": lambda text, arg: schedule.schedule(
        text, latency=int(arg) if arg else None
    )
}
#: L2 -> L2 passes, node body to node body.
L2_PASSES = {"retile": lambda body, arg: l2_passes_mod.retile(body, arg or "auto")}
DEFAULT_L1 = ("schedule",)
DEFAULT_L2 = ("retile=auto",)
#: Options by stage: the L3 plan's and the L2 lowering's.
PLAN_OPTIONS = ("softmax",)
LOWER_OPTIONS = ("lag", "order")


def _split(p: str) -> tuple:
    name, _, arg = p.partition("=")
    return name, arg or None


def l2_text(module, name: str, passes=DEFAULT_L2) -> str:
    """`name`'s L2 body after the L2 `passes`, as a module text (target and
    the body), each pass's output read back and verified."""
    body = l2_reader.body(module, name)
    l2_verify.check(body, UNITS)
    head = f"target {module.target or 'ktpu.v9'}\n\n"
    text = head + body_text(body)
    for p in passes:
        pname, arg = _split(p)
        body = L2_PASSES[pname](body, arg)
        text = head + body_text(body)
        body = l2_reader.body(read_module(text, f"<{name}.l2 {p}>"), name)
        l2_verify.check(body, UNITS)
    return text


def compile_l2(module, name: str, passes=DEFAULT_L2, options=None) -> str:
    """L1 text of `name`'s L2 body: the L2 passes, then lowered with the
    lowering `options` (`{"lag": 1}`)."""
    m = read_module(l2_text(module, name, passes), f"<{name}.l2>")
    body = l2_reader.body(m, name)
    opts = {k: v for k, v in (options or {}).items() if k in LOWER_OPTIONS}
    t = transposed(module, name)
    if t:
        opts["transposed"] = t
    return l2_lower.lower(module, name, body, **opts)


def transposed(module, name: str) -> tuple:
    """Parameters `name.l3` reads only through `transpose`: their L2 (and L1)
    memory holds the transpose of the host's value."""
    if (name, "l3") not in module.fns:
        return ()
    fn = l3_module(module).fns[name]
    readers: dict = {}
    for s in fn.body:
        for a in getattr(s, "args", ()):
            if hasattr(a, "name"):
                readers.setdefault(a.name, []).append(s.op)
    return tuple(p for p, _ in fn.params if readers.get(p) == ["transpose"])


def l1_passes(text: str, passes=DEFAULT_L1) -> str:
    for p in passes:
        pname, arg = _split(p)
        text = L1_PASSES[pname](text, arg)
    return text


def compile_l3(module, name: str, options=None) -> str:
    """The L2 module text planned from `name.l3`, read back and verified."""
    text = plan_l2(
        module, name, {k: v for k, v in (options or {}).items() if k in PLAN_OPTIONS}
    )
    body = l2_reader.body(read_module(text, f"<{name}.l3 planned>"), name)
    l2_verify.check(body, UNITS)
    return text


def compile_l1(
    module, name: str, level: str, l1=DEFAULT_L1, l2=DEFAULT_L2, options=None
) -> str:
    """L1 text of `name` compiled from its body at `level` (`l2` or `l3`):
    the L3 plan, L2 passes, lowering, L1 passes."""
    if level == "l3":
        module = _with_l2(module, name, compile_l3(module, name, options))
        level = "l2"
    if level == "l2":
        return l1_passes(compile_l2(module, name, l2, options), l1)
    raise ValueError(f"no compile from {level}")


def _with_l2(module, name: str, text: str):
    """`module` with `name.l2` replaced by the planned body."""
    planned = read_module(text, f"<{name}.l3 planned>")
    out = copy.copy(module)
    out.fns = {**module.fns, (name, "l2"): planned.fns[(name, "l2")]}
    out.source = planned.source
    out.file = planned.file
    return out


__all__ = [
    "DEFAULT_L1",
    "DEFAULT_L2",
    "L1_PASSES",
    "L2_PASSES",
    "UNITS",
    "compile_l1",
    "compile_l2",
    "l1_passes",
    "l2_text",
]
