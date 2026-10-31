"""One lanes class, with padded duplicate members and fixed tap instructions."""

import numpy as np

from kohakutpu.application.kernels import lanes as L
from kohakutpu.application.kernels import vector as V
from kohakutpu.language import dsl as kt


def tables(plan):
    """Each padded member repeats the last real member, including its destination."""
    offset, first = 0, 0
    for cls in plan.classes:
        taps = len(cls.taps)
        nr = -(-L.rows(cls) // 8) * 8
        old = plan.index[offset : offset + (taps + 1) * 8 * L.entries(cls)].reshape(
            taps + 1, 8, L.entries(cls)
        )
        index = np.empty((taps + 1, 8, nr), np.float32)
        for k in range(nr * 8):
            r, j = divmod(min(k, cls.words - 1), 8)
            dest_r, dest_j = divmod(k, 8)
            index[:, dest_j, dest_r] = old[:, j, r]
        yield cls, index.reshape(-1), plan.masks[first : first + taps].copy()
        offset += old.size
        first += taps


def kernel(cls, src_words, dst_words, cores, core_policy="all", rotations=None):
    if (
        not cls.taps
        or cls.words <= 0
        or cores <= 0
        or core_policy not in ("all", "min")
    ):
        raise ValueError("invalid lanes class")
    nr = -(-L.rows(cls) // 8) * 8
    trips = nr // L.ROWS
    used = cores if core_policy == "all" else min(cores, trips)
    padded = -(-trips // used) * used * L.ROWS
    taps = len(cls.taps)
    ports = {
        "src": kt.In(src_words, 16),
        "dst": kt.Out(dst_words, 16),
        "index": kt.In((taps + 1) * 8 * nr, dtype="f32"),
        "mask": kt.In(taps, 128),
    }
    given = {
        "src": kt.TensorSpec.make((src_words, 16)),
        "dst": kt.TensorSpec.make((dst_words, 16)),
        "index": kt.TensorSpec.make(((taps + 1) * 8 * nr,), "f32"),
        "mask": kt.TensorSpec.make((taps, 128)),
    }

    def body(src, dst, index, mask):
        scratch = kt.buffer(((taps + 1) * padded, 128), "f16")
        inputs = [L._Rows(scratch, t * padded) for t in range(taps)]
        out = L._Rows(scratch, taps * padded)
        for j in range(8):
            col = slice(j * 16, (j + 1) * 16)
            for t, rows in enumerate(inputs):
                start = (t * 8 + j) * nr
                kt.gather(rows[0:nr, col], src, index[start : start + nr], fp=True)
            if cls.keep:
                start = (taps * 8 + j) * nr
                kt.gather(out[0:nr, col], dst, index[start : start + nr], fp=True)
        for first in range(0, taps, L.GROUP):
            group = range(first, min(first + L.GROUP, taps))
            for core in kt.par(used, on=kt.vc, classed=True):
                masks = {t: kt.unpack(kt.load(mask[t : t + 1, :])) for t in group}
                for step in kt.range((trips + used - 1 - core) // used):
                    row = (core + step * used) * L.ROWS
                    base = (
                        kt.unpack(kt.load(out[row : row + L.ROWS, :]))
                        if cls.keep or first
                        else 0.0
                    )
                    for t in group:
                        rot = cls.taps[t][0]
                        one = 2 if rot == 1 else 1
                        two = (rot - one) % 16
                        if rotations is not None:
                            one, two = rotations[t]
                        value = kt.unpack(kt.load(inputs[t][row : row + L.ROWS, :]))
                        value = kt.xrot(kt.xrot(value, one), two)
                        base = kt.where(masks[t], value, base)
                    kt.store(out[row : row + L.ROWS, :], base)
        for j in range(8):
            start = (taps * 8 + j) * nr
            kt.scatter(
                dst,
                out[0:nr, j * 16 : (j + 1) * 16],
                index[start : start + nr],
                fp=True,
            )

    result = kt.Kernel.build(
        V.name("lanes_class", cls, src_words, dst_words, cores, core_policy),
        body,
        ports,
    )
    result.class_info = {
        "taps": taps,
        "keep": cls.keep,
        "rows": nr,
        "cores": used,
        "padded_words": nr * 8 - cls.words,
    }
    return result, given
