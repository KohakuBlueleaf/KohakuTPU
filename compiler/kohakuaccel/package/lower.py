"""Per-call payloads into the framework pipeline, and out as package steps.

One stage's ``{instance: [words]}`` becomes pinned tasks compiled with the
host's round bounds lifted (package-format.md §7).
"""

from dataclasses import replace

from kohakuaccel.backend.slots import Prebuilt
from kohakuaccel.compile import compile
from kohakuaccel.dispatch import deal
from kohakuaccel.ir.l2 import Policy, ScheduleIR
from kohakuaccel.package.format import type_code, unit_word

#: What Pack sees in place of the host's staging/command/FIFO bounds.
UNBOUNDED = 1 << 30


def schedule(
    payloads: dict, unit: str, nodes, acks=None, mesh: int | None = None
) -> ScheduleIR:
    """One stage's instances as a schedule of pinned tasks."""
    placed = deal(payloads, nodes)
    acks = acks or {}
    s = ScheduleIR()
    for key, words in sorted(payloads.items()):
        owed = acks.get(key)
        s.task(
            unit,
            payload=list(words),
            flits=len(words),
            signals=len(words),
            policy=Policy.PINNED,
            coord=tuple(placed[key]),
            mesh=mesh,
            acks=((tuple(owed[0]), owed[1]),) if owed else (),
        )
    return s


def compile_stage(payloads: dict, unit: str, nodes, machine, fields=None, acks=None):
    """The framework compile of one stage: returns the pipeline's `Result`."""
    roomy = replace(
        machine, stage_flits=UNBOUNDED, ncmd=UNBOUNDED, inst_depth=UNBOUNDED
    )
    return compile(
        schedule(payloads, unit, nodes, acks, machine.default), roomy, Prebuilt(fields)
    )


def machine_units(machine, mesh: int | None = None) -> list[int]:
    """Every unit on one mesh of `machine`, as package unit words."""
    here = machine.mesh(mesh)
    return [
        unit_word(type_code(kind), x, y, here.index)
        for kind, coords in sorted(here.units.items())
        for x, y in coords
    ]


def unit_types(machine, mesh: int | None = None) -> dict:
    """Coordinate -> unit type name, for one mesh."""
    here = machine.mesh(mesh)
    return {tuple(c): kind for kind, coords in here.units.items() for c in coords}
