"""L2, the schedule (docs/spec/l2-schedule.md).

`schedule` is L2: buffers with project layouts, work items, placement, packages.
`legacy` is the old pass pipeline's L2 (encoded payloads, rounds), kept for its
callers until they are gone. The L2 -> L1 compiler is `kohakuaccel.ir.l2.lower`,
imported by its own path: it lowers through `kohakuaccel.package`, which the
backend layer imports, which imports this package.
"""

from kohakuaccel.ir.l2.legacy import Coord, Policy, Region, Round, ScheduleIR, Task
from kohakuaccel.ir.l2.schedule import Buffer, Item, Schedule, ScheduleError, View

__all__ = [
    "Buffer",
    "Coord",
    "Item",
    "Policy",
    "Region",
    "Round",
    "Schedule",
    "ScheduleError",
    "ScheduleIR",
    "Task",
    "View",
]
