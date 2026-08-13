"""The upper seam: the four shapes a frontend composes.

    spread    one domain, N independent tasks
    chain     stage k feeds stage k+1
    gather    many tasks reduce into one
    iterate   repeat a body with a barrier between iterations

Each returns the task indices it created.
"""

import itertools

from kohakuaccel.ir.l2 import Policy, ScheduleIR


def spread(schedule: ScheduleIR, pieces, make, policy=Policy.FREE) -> list[int]:
    """One domain, N independent tasks: the data-parallel shape.

    `make` is called as ``make(piece, i)`` and returns the keyword arguments for
    :meth:`ScheduleIR.task`. Returns the task indices created.
    """
    out = []
    for i, piece in enumerate(pieces):
        spec = dict(make(piece, i))
        spec.setdefault("policy", policy)
        out.append(schedule.task(**spec))
    return out


def chain(schedule: ScheduleIR, stages, make, policy=Policy.FREE) -> list[int]:
    """Stage k feeds stage k+1: the pipeline shape.

    Consecutive tasks are made dependent, so they land in consecutive rounds.
    Returns the task indices created.
    """
    out = spread(schedule, stages, make, policy)
    for a, b in itertools.pairwise(out):
        schedule.depends(a, b)
    return out


def gather(schedule: ScheduleIR, sources: list[int], make, policy=Policy.FREE) -> int:
    """Many tasks reduce into one: the reduction shape.

    `make` is called as ``make(sources)``. The result depends on every source, so
    it is dispatched in a later round than all of them.
    """
    spec = dict(make(sources))
    spec.setdefault("policy", policy)
    sink = schedule.task(**spec)
    for i in sources:
        schedule.depends(i, sink)
    return sink


def iterate(schedule: ScheduleIR, count: int, body) -> list[list[int]]:
    """Repeat `body` with a barrier between iterations.

    `body` is called as ``body(k)`` and returns the task indices that iteration
    produced. Every task of iteration k precedes every task of k+1. Returns the
    indices produced, per iteration.
    """
    rounds = []
    previous: list[int] = []
    for k in range(count):
        made = list(body(k))
        for a in previous:
            for b in made:
                schedule.depends(a, b)
        rounds.append(made)
        previous = made
    return rounds
