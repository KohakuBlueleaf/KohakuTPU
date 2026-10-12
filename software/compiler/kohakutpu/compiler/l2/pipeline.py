"""L2 -> L2 passes, L2 -> L1, L1 -> L1, in order."""

from typing import cast

from kohakutpu.compiler import target as T
from kohakutpu.compiler.l2 import lower
from kohakutpu.compiler.l2 import passes as P
from kohakutpu.compiler.opt import schedule as S
from kohakutpu.language.text import module as M

PASSES = ("place", "space", "share", "split", "pipeline", "retile")


def compile_l1(
    module, name: str, machine=None, passes=PASSES, schedule=True
) -> M.Module:
    """The L1 module for `fn name.l2`: `passes` in order, the lowering, then
    the image scheduler when `schedule`. `machine` defaults to a four-node
    card (a body uses the nodes it names)."""
    machine = machine or T.machine(nodes=4)
    m = P.run(module, name, passes, machine)
    l1 = M.read(lower.lower(m, name, machine), f"{module.file}:{name}.l1")
    return cast(M.Module, S.schedule_module(l1)) if schedule else l1


__all__ = ["PASSES", "compile_l1"]
