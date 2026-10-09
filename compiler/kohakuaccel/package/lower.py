"""Per-call payloads into the framework pipeline, and out as package steps.

One stage's ``{instance: [words]}`` becomes pinned tasks compiled with the
host's round bounds lifted (package-format.md §7).
"""

from dataclasses import replace

from kohakuaccel.backend.slots import Prebuilt
from kohakuaccel.compile import compile
from kohakuaccel.dispatch import deal
from kohakuaccel.ir.l2 import Policy, ScheduleIR
from kohakuaccel.package.units import machine_units, unit_types

__all__ = ["compile_stage", "machine_units", "schedule", "unit_types"]

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
