"""Linear, pre-composition capacity planning from eager package accounting.

The estimate is deliberately separate from final admission. It reads the ordinary
(pre-ENGINE) package: counting ENGINE steps would hide the writer's earlier u16
step limit. The planner never invokes a compiler or a verifier.
"""

import struct
from dataclasses import asdict, dataclass

from kohakutpu.application.runtime.graph.limits import CapacityExceeded, PackageUsage
from kohakutpu.application.runtime.os import abi as A
from kohakutpu.application.runtime.packages import Prebuilt, placed


@dataclass(frozen=True)
class LaunchUsage:
    usage: PackageUsage
    units: frozenset

    @classmethod
    def read(cls, raw):
        usage = PackageUsage.read(raw)
        header = struct.unpack_from(A.PKG_HDR_FMT, raw)
        # A library is allowed to store an already lowered package. Its header
        # cannot predict the earlier writer's step count; isolate that launch
        # instead of interpreting one ENGINE step as one source step.
        if any(
            raw[header[A.KA_PH_OFF_STEP] + 16 * i] == A.KA_OP_ENGINE
            for i in range(usage.steps)
        ):
            return None
        # The first word identifies a unit; credits/fetch state in the second
        # word are not a second physical participant.
        units = frozenset(
            struct.unpack_from("<Q", raw, header[A.KA_PH_OFF_UNIT] + 16 * i)[0]
            for i in range(usage.units)
        )
        return cls(usage, units)


def capture_usage(programs, nodes):
    """Capture exact eager-instance counts, not a class template's placeholder.

    Binding without ENGINE lowering preserves the stage whose writer enforces
    L0.PKG.011. No whole graph is constructed here.
    """
    result = {}
    for node, program in placed(programs, nodes).items():
        if not isinstance(program, Prebuilt):
            return None
        counted = LaunchUsage.read(program.bound(False))
        if counted is None:
            return None
        result[node] = {"usage": asdict(counted.usage), "units": sorted(counted.units)}
    return result or None


def recorded_usage(value):
    """The recording sidecar stays JSON-compatible and equality-comparable."""
    if value is None:
        return None
    return {
        int(node): LaunchUsage(PackageUsage(**item["usage"]), frozenset(item["units"]))
        for node, item in value.items()
    }


@dataclass(frozen=True)
class PartPlan:
    intervals: tuple
    participants: tuple
    launches: int
    # Counts planner visits, including a launch retried at a new part's front.
    # This measures planning only, not total compiler work.
    visits: int


def plan_parts(usages, machine, *, max_launches=None, participants=()):
    """Maximal consecutive estimated-fitting intervals, before any composition.

    Missing accounting (e.g. a newly fused or trace-only launch) is isolated in
    its own part. It must never cause an unbounded speculative whole compile.
    Exact per-part writer/receiver checks still decide admission.
    """
    count = len(usages)
    if not count:
        raise ValueError("a retained plan needs at least one launch")
    if max_launches is not None and (type(max_launches) is not int or max_launches < 1):
        raise ValueError("retained part size must be a positive launch count")
    nodes = tuple(
        sorted(
            set(participants).union(
                node for usage in usages if usage is not None for node in usage
            )
        )
    )
    if not nodes:
        raise ValueError("a retained plan needs physical participants")
    coordinator = nodes[0]
    intervals, first, totals, identities = [], 0, {}, {}
    visits = 0

    def append(index, current, units):
        nonlocal visits
        visits += 1
        candidate, ids = {}, {}
        for node in nodes:
            own = usages[index].get(node)
            previous = current.get(node, PackageUsage(0, 0, 0, 0, 0))
            incoming = own.usage if own is not None else PackageUsage(0, 0, 0, 0, 0)
            merged = units.get(node, frozenset()) | (own.units if own else frozenset())
            # Each nonterminal global launch retains its own gather/release,
            # even when it is terminal in this capacity part. A unit's eager
            # terminal drain is already included in its package counts.
            join = 0
            if index + 1 < count and len(nodes) > 1:
                join = len(nodes) if node == coordinator else 2
            # A fresh part terminates after a remote handshake, so account for
            # its final local BARRIER as well. Replacing this reservation on
            # extension avoids accumulating one extra terminal per launch.
            terminal = int(len(nodes) > 1 and index + 1 < count)
            old_terminal = int(bool(current) and len(nodes) > 1)
            candidate[node] = PackageUsage(
                units=len(merged),
                buffers=previous.buffers + incoming.buffers,
                steps=previous.steps
                - old_terminal
                + incoming.steps
                + join
                + terminal
                + int(own is None),
                relocations=previous.relocations + incoming.relocations,
                payloads=previous.payloads + incoming.payloads,
                ack_reserve=max(previous.ack_reserve, incoming.ack_reserve),
            )
            # Eager inputs may be relocatable. Relocations are size-accounted
            # here, and required to be bound by final artifact admission.
            candidate[node].check(machine, node, bound=False)
            ids[node] = merged
        return candidate, ids

    index = 0
    while index < count:
        if usages[index] is None:
            if first < index:
                intervals.append((first, index))
            intervals.append((index, index + 1))
            first = index = index + 1
            totals, identities = {}, {}
            continue
        if max_launches is not None and index - first == max_launches:
            intervals.append((first, index))
            first, totals, identities = index, {}, {}
        try:
            following, following_ids = append(index, totals, identities)
        except CapacityExceeded:
            if first == index:
                # One eager launch plus its join is indivisible. Let its exact
                # compile/admission decide: summing buffers can overestimate a
                # bound package, but never retry a larger interval here.
                intervals.append((index, index + 1))
                first = index = index + 1
                totals, identities = {}, {}
            else:
                intervals.append((first, index))
                first, totals, identities = index, {}, {}
        else:
            totals, identities = following, following_ids
            index += 1
    if first < count:
        intervals.append((first, count))
    return PartPlan(tuple(intervals), nodes, count, visits)
