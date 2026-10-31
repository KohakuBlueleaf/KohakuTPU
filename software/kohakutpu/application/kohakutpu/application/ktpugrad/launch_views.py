"""Index mapping through an owned snapshot view descriptor."""

from kohakutpu.application.ktpugrad import uops as U
from kohakutpu.application.ktpugrad.lower import Access


def _remap_view_access(a: Access, vkey: tuple) -> tuple | None:
    """Remap access `a` through a view form `vkey` when each range steps along
    one axis of the view's logical shape."""
    _, _, _, _, vshape, vcoef, voff = vkey
    if not vshape or len(vshape) != len(vcoef):
        return None
    # Dense place values of vshape
    places = []
    step = 1
    for d in reversed(vshape):
        places.insert(0, step)
        step *= d
    rem = a.off
    new_off = voff
    coords = []
    for d, p, (_, vs) in zip(vshape, places, vcoef):
        q, rem = divmod(rem, p)
        if q < 0 or q >= d:
            return None
        coords.append(q)
        new_off += q * vs
    if rem != 0:
        return None
    new_coef = []
    axis_spans = [0] * len(vshape)
    for r, c in a.coef:
        ext = U.extent(r)
        if ext <= 1 or c == 0:
            new_coef.append((r, 0))
            continue
        matched = None
        for j, (d, p, (_, vs)) in enumerate(zip(vshape, places, vcoef)):
            if c % p == 0:
                step_j = c // p
                if 0 <= coords[j] + (axis_spans[j] + step_j * (ext - 1)) < d:
                    matched = (j, step_j, vs)
                    break
        if matched is None:
            return None
        j, step_j, vs = matched
        axis_spans[j] += step_j * (ext - 1)
        new_coef.append((r, step_j * vs))
    return tuple(new_coef), new_off
