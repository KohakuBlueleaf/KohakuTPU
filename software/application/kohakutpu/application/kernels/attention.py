"""Masked dense attention on explicitly padded MXFP7 operands."""

from collections import OrderedDict

from kohakutpu.language import dsl as kt
from kohakutpu.language.examples.dsl.cluster.sweep import core_blocks, sweep
from kohakutpu.language.numerics import rounding

MASK_KEYS = True
KERNELS_KEPT = 256
_kernels: OrderedDict = OrderedDict()


def kernel(heads, sq, tk_total, dim, tq=64, tk=64, precise_scores=False):
    """Physical operands are bounded padded forms; valid marks logical keys."""
    kkey = (
        heads,
        sq,
        tk_total,
        dim,
        tq,
        tk,
        precise_scores,
        MASK_KEYS,
        kt.Kernel.build,
    )
    hit = _kernels.get(kkey)
    if hit is not None:
        _kernels.move_to_end(kkey)
        return hit
    ports = {
        "q": kt.In(heads * sq, dim, dtype="mx7a", layout=dict(gm=tq // 4, nk=2)),
        "k": kt.In(heads * tk_total, dim, dtype="mx7b", layout=dict(gn=tk // 4, nk=2)),
        "vt": kt.In(
            heads * dim, tk_total, dtype="mx7b", layout=dict(gn=dim // 4, nk=2)
        ),
        "valid": kt.In(heads * sq, tk_total, layout=dict(tile=(tq // 4, tk // 4))),
        "o": kt.Out(heads * sq, dim),
    }

    dp = -(-dim // 128) * 128
    if precise_scores:
        ports["q"] = kt.In(heads * sq * 16, dp)
        ports["k"] = kt.In(heads * tk_total, dp)

    def body(q, k, vt, valid, o):
        srows = heads * sq
        scores = kt.buffer((srows, tk_total), "f16", tile=(tq // 4, tk // 4))
        probs = kt.buffer((srows, tk_total), "mx7a", gm=tq // 4, nk=2)
        low = (
            kt.buffer((srows, tk_total), "mx7a", gm=tq // 4, nk=2)
            if precise_scores
            else None
        )
        low_product = (
            kt.buffer((srows, dim), "f16", tile=(tq // 4, dim // 4))
            if precise_scores
            else None
        )
        nb = tk_total // tk
        maxima = kt.buffer((nb, srows), "f16")
        sums = kt.buffer((nb, srows), "f16")
        rowmax = kt.buffer((srows,), "f16")
        rowsum = kt.buffer((srows,), "f16")
        product = kt.buffer((srows, dim), "f16", tile=(tq // 4, dim // 4))
        scale = dim**-0.5 * kt.LOG2E
        if precise_scores:
            raw = kt.buffer((srows, tk_total), "f16")
            qg = kt.buffer((srows * 16, dp), "f16", tile=(4, dp // 4))
            kg = kt.buffer((heads * tk_total, dp), "f16", tile=(4, dp // 4))
            kt.gt4(qg, q)
            kt.gt4(kg, k)
            for head in range(heads):
                for block in kt.par(tk_total // 16, on=kt.vc, at=kt.ANY):
                    kv = kt.unpack(
                        kt.load(
                            kg[
                                head * tk_total
                                + block * 16 : head * tk_total
                                + block * 16
                                + 16,
                                :,
                            ]
                        )
                    )
                    for qi in kt.range(sq):
                        qv = kt.load(
                            qg[(head * sq + qi) * 16 : (head * sq + qi) * 16 + 16, :]
                        )
                        kt.store(
                            raw[head * sq + qi, block * 16 : block * 16 + 16],
                            kt.rsum(kv * qv),
                        )
            kt.gt4(scores, raw)
        else:
            sweep(q, k, scores, tq, tk, groups=heads)
        for b in range(nb):
            for r0, _, rper in core_blocks(srows, tk, tk, 16, tq):
                for t in kt.range(rper // 16):
                    r = r0 + t * 16
                    mask = kt.load(valid[r : r + 16, b * tk : (b + 1) * tk])
                    x = kt.load(scores[r : r + 16, b * tk : (b + 1) * tk]) * scale
                    if MASK_KEYS:
                        x = kt.where(mask, x, float("-inf"))
                    kt.store(maxima[b, r : r + 16], kt.rmax(x))
        for r0, _, rper in core_blocks(srows, 32, 32, 16, tq):
            for t in kt.range(rper // 16):
                r = r0 + t * 16
                m = kt.unpack(kt.load(maxima[0, r : r + 16]))
                for b in range(1, nb):
                    m = kt.maximum(m, kt.load(maxima[b, r : r + 16]))
                kt.store(rowmax[r : r + 16], m)
        for b in range(nb):
            for r0, _, rper in core_blocks(srows, tk, tk, 16, tq):
                for t in kt.range(rper // 16):
                    r = r0 + t * 16
                    mask = kt.load(valid[r : r + 16, b * tk : (b + 1) * tk])
                    x = kt.load(scores[r : r + 16, b * tk : (b + 1) * tk]) * scale
                    if MASK_KEYS:
                        x = kt.where(mask, x, float("-inf"))
                    p = kt.exp2(x - kt.load(rowmax[r : r + 16]))
                    if precise_scores:
                        hi = rounding.trunc(p * 32.0, kt, kt.machine().sig_bits) * (
                            1.0 / 32.0
                        )
                        kt.store(probs[r : r + 16, b * tk : (b + 1) * tk], hi)
                        kt.store(low[r : r + 16, b * tk : (b + 1) * tk], p - hi)
                    else:
                        kt.store(probs[r : r + 16, b * tk : (b + 1) * tk], p)
                    kt.store(sums[b, r : r + 16], kt.rsum(p))
        for r0, _, rper in core_blocks(srows, 32, 32, 16, tq):
            for t in kt.range(rper // 16):
                r = r0 + t * 16
                z = kt.unpack(kt.load(sums[0, r : r + 16]))
                for b in range(1, nb):
                    z = z + kt.load(sums[b, r : r + 16])
                kt.store(rowsum[r : r + 16], z)
        sweep(probs, vt, product, tq, dim, groups=heads)
        if precise_scores:
            sweep(low, vt, low_product, tq, dim, groups=heads)
        for r0, _, rper in core_blocks(srows, dim, dim, 16, tq):
            for t in kt.range(rper // 16):
                r = r0 + t * 16
                value = kt.load(product[r : r + 16, :])
                if precise_scores:
                    value = value + kt.load(low_product[r : r + 16, :])
                kt.store(
                    o[r : r + 16, :], value * kt.recip(kt.load(rowsum[r : r + 16]))
                )

    got = kt.Kernel.build("attention_partial", body, ports)
    if KERNELS_KEPT > 0:
        _kernels[kkey] = got
        while len(_kernels) > KERNELS_KEPT:
            _kernels.popitem(last=False)
    return got
