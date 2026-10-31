"""Bounded horizontal groups with no inter-member storage dependencies."""

from kohakutpu.application.runtime.forms import base_of


def spans(view):
    """Include all representations and their owners; views conservatively cover the base."""
    storage, _ = base_of(view)
    result = {(storage.addr, storage.addr + storage.nbytes)}
    for form in storage.forms.values():
        result.add((form.addr, form.addr + form.nbytes))
    return result


def overlap(left, right):
    return any(a < d and c < b for a, b in left for c, d in right)


def groups(items, eligible, ignore_hazards=False):
    """Pair adjacent same-domain elementwise members without reordering the schedule."""
    access = []
    for plan, buffers in items:
        if plan is None or plan.out_slot not in buffers:
            access.append((set(), set()))
            continue
        writes = spans(buffers[plan.out_slot])
        reads = set()
        for operand in plan.accesses:
            if operand.slot is not None and operand.slot in buffers:
                reads.update(spans(buffers[operand.slot]))
        access.append((reads, writes))
    result = []
    i = 0
    while i < len(items):
        if i + 1 < len(items):
            a, b = items[i][0], items[i + 1][0]
            ar, aw = access[i]
            br, bw = access[i + 1]
            hazard = overlap(aw, br | bw) or overlap(bw, ar)
            if (
                eligible(a)
                and eligible(b)
                and a.out_shape == b.out_shape
                and a.out_dtype == b.out_dtype
                and aw
                and bw
                and (ignore_hazards or not hazard)
            ):
                result.append(([i, i + 1], set()))
                i += 2
                continue
        result.append(([i], set()))
        i += 1
    return result
