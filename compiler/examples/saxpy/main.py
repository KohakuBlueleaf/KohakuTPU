"""Compile saxpy: a frontend of one `spread`, and the framework does the rest.

Run with ``python -m examples.saxpy.main`` from ``compiler/``.
"""

import json
import sys

from kohakuaccel.compile import compile
from kohakuaccel.frontend import spread
from kohakuaccel.ir import Region, ScheduleIR
from kohakuaccel.machinespec import MachineSpec

from .backend import SaxpyBackend
from .isa import ISA

ELEM = 4  # float32
X_ADDR = 0x0001_0000
Y_ADDR = 0x0002_0000

MACHINE = MachineSpec(
    name="saxpy-2",
    units={"SX": ((1, 0), (2, 0))},
    stage_flits=128,
    ncmd=128,
    inst_depth=32,
    mem_ports=((0, 0),),
)


def frontend(n: int, a: float, pieces: int) -> ScheduleIR:
    """Split `n` elements into `pieces` independent tasks.

    The whole frontend. Declaring `reads` and `writes` is what earns the
    dependency edges and the coalescing pass without stating either.
    """
    schedule = ScheduleIR()
    base, extra = divmod(n, pieces)
    spans, off = [], 0
    for i in range(pieces):
        count = base + (1 if i < extra else 0)
        if count:
            spans.append((off, count))
            off += count

    spread(
        schedule,
        spans,
        lambda span, i: dict(
            unit_type="SX",
            label=f"saxpy[{span[0]}:{span[0] + span[1]}]",
            payload={
                "n": span[1],
                "a": a,
                "x_addr": X_ADDR + span[0] * ELEM,
                "y_addr": Y_ADDR + span[0] * ELEM,
            },
            reads=(Region(X_ADDR + span[0] * ELEM, span[1] * ELEM),),
            writes=(Region(Y_ADDR + span[0] * ELEM, span[1] * ELEM),),
        ),
    )
    return schedule


def build(n: int = 64, a: float = 2.5, pieces: int = 2):
    """Compile one saxpy and return the whole result."""
    return compile(frontend(n, a, pieces), MACHINE, SaxpyBackend())


def main() -> int:
    if "--json" in sys.argv:
        json.dump(build().artifact.to_dict(), sys.stdout)
        return 0

    schedule = frontend(n=64, a=2.5, pieces=2)
    result = compile(schedule, MACHINE, SaxpyBackend())

    print(schedule.pretty())
    print()
    print(result.program.pretty())
    print()
    print(result.report())
    print()
    print("disassembled:")
    for i, payload in enumerate(result.artifact.flits):
        print(f"  slot {i}: {ISA.disasm(payload)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
