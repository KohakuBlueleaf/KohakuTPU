"""Collective schedules: which adjacent hop happens, and in what order.

Fabric-generic and pure. A hop is a pair of positions and nothing here knows
what crosses one -- :meth:`~kohakuaccel.machinespec.MachineSpec.path` says which
positions are adjacent, and this says in what order to walk them.
"""


def converge(walk, into) -> list:
    """The adjacent hops summing every position on `walk` into `into`, in order.

    Both arms walk INWARD from the ends, so a hop's source has already absorbed
    everything beyond it and every hop crosses one link -- which holds on a
    ring, on a chain, and on a chain that forwards.

    Returns ``(src, dst)`` pairs. Raises :class:`ValueError` unless `into` is
    on the walk.
    """
    walk = list(walk)
    if into not in walk:
        raise ValueError(f"{into!r} is not on the path {walk}")
    at = walk.index(into)
    inward = [(walk[j], walk[j + 1]) for j in range(at)]
    inward += [(walk[j], walk[j - 1]) for j in range(len(walk) - 1, at, -1)]
    return inward


def hops_to(walk, into) -> int:
    """Links the LONGEST arm of :func:`converge` crosses.

    The reduce's depth, and its latency once hops overlap. Converging inward
    halves it against every rank sending to one place; the two schedules are
    the same when the destination is an end.
    """
    at = list(walk).index(into)
    return max(at, len(walk) - 1 - at)
