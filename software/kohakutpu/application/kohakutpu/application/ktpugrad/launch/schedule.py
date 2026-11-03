"""Launch groups: elementwise members over one domain gathered into one package.

A group starts at an eligible member and takes each later eligible member of its
domain (`out_n`, `out_dtype`) within `KTPU_FUSE_WINDOW` items, up to
`KTPU_FUSE_MEMBERS` members. A member may read or overwrite an earlier member's
output (the package runs them in order); it moves ahead of the items it passes
only when it touches nothing they write and writes nothing they touch. An item
whose buffers are not known (no plan) ends the scan.
"""

from kohakutpu.application.runtime.forms import base_of
from kohakutpu.language.config import ENV


def spans(view):
    """Include all representations and their owners; views conservatively cover the base."""
    storage, _ = base_of(view)
    result = {(storage.addr, storage.addr + storage.nbytes)}
    for form in storage.forms.values():
        result.add((form.addr, form.addr + form.nbytes))
    return result


def overlap(left, right):
    return any(a < d and c < b for a, b in left for c, d in right)


def _access(plan, buffers) -> tuple | None:
    """`(writes, touched)` spans (touched: every buffer it names); None for an
    item with no plan."""
    if plan is None or plan.out_slot not in buffers:
        return None
    writes = spans(buffers[plan.out_slot])
    return writes, set().union(*(spans(b) for b in buffers.values()))


def groups(items, eligible, ignore_hazards=False):
    """`items` (`(plan, buffers)`) as `(indices, dead spans)` in launch order."""
    window = int(ENV.get("KTPU_FUSE_WINDOW", "32"))
    cap = int(ENV.get("KTPU_FUSE_MEMBERS", "8"))
    access = [_access(p, b) for p, b in items]
    taken = [False] * len(items)
    result = []
    for i, (head, _) in enumerate(items):
        if taken[i]:
            continue
        taken[i] = True
        grp = [i]
        own = access[i]
        if eligible(head) and own is not None and own[0]:
            passed_w, passed_t = set(), set()
            for j in range(i + 1, min(len(items), i + 1 + window)):
                other = access[j]
                if len(grp) == cap or other is None:
                    break
                if taken[j]:
                    continue
                plan = items[j][0]
                writes, touched = other
                joins = (
                    eligible(plan)
                    and bool(writes)
                    and plan.out_n == head.out_n
                    and plan.out_dtype == head.out_dtype
                    and (
                        ignore_hazards
                        or not (overlap(writes, passed_t) or overlap(touched, passed_w))
                    )
                )
                if joins:
                    grp.append(j)
                    taken[j] = True
                else:
                    passed_w |= writes
                    passed_t |= touched
        result.append((grp, set()))
    return result
