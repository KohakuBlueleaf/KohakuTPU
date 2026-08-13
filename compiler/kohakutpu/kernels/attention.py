"""Flash attention: MHA, optional causal, optional GQA.

Heads ride the batch axis, so a `(B, H, L, D)` operand needs no head loop. GQA
stacks the query heads of one KV head on the row axis instead -- `groups` of
them -- which is what lets one kernel serve both.

THE QUERY AXIS IS NOT BLOCKED unless asked. `qblock=None` streams keys against
the whole query length, which is the old DSL's shape and costs the fewest DRAM
round trips; a `qblock` bounds the arena instead, at one extra pass per key
block. `causal` needs one, since a query block is what selects the keys.

Four stages per key block, and two of them are forced: `weights` is computed on
a vector core and multiplied on a cluster, and VC -> cluster L1 cannot carry
MXFP7 (`.plan/HARDWARE-WANTS.md` §1). The other two are one band each.
"""

import numpy as np
from kohakuaccel.lang import ceildiv, dims, loop, units
from kohakutpu.lang import kernel

from kohakutpu import lang as L

Lq, Lkv, Dqk, Dv = dims("Lq, Lkv, Dqk, Dv")

#: Low enough that `exp2` of any real score minus it is zero in fp16, and high
#: enough not to become an inf once multiplied by a correction.
NEG = -60000.0


@kernel
def flash_attention(
    q=L.In(..., Lq, Dqk),
    k=L.In(..., Lkv, Dqk),
    v=L.In(..., Dv, Lkv),
    o=L.Out(..., Lq, Dv),
    *,
    block=64,
    qblock=None,
    causal=False,
    keys=None,
    groups=1,
    gm=8,
    gn=8,
    nk=2,
    part=8192,
):
    """``softmax(q @ k.T) @ v``, streamed over key blocks. Needs ``block == Dv``.

    `q` arrives PRE-SCALED by ``log2(e) / sqrt(Dqk)``, `v` transposed as
    ``(Dv, Lkv)``. `Lkv` must be a multiple of `block`, `Lq` of `qblock`.

    `groups` is GQA: query heads per KV head, stacked on the row axis, so `q`
    holds ``groups * Lq`` rows where `k` and `v` hold one head's. `causal` is a
    COMPILE-TIME flag needing `qblock` and ``Lq == Lkv``. `keys` is how many of
    `Lkv` are real. Raises :class:`ValueError` for combinations with no mask.
    """
    # A mask is one block square, so it forces the query span to match. Defaulted
    # rather than demanded: there is only one value that works.
    if (causal or keys is not None) and qblock is None:
        qblock = block
    if causal and qblock != block:
        raise ValueError(
            "causal attention selects key blocks by QUERY block and its mask is "
            f"one block square, so it needs `qblock=block`; got {qblock!r}"
        )
    if causal and groups != 1:
        raise ValueError(
            "causal with groups > 1 would index a query block modulo its group, "
            "and a symbolic block index has no modulo here; call once per group"
        )
    tail = 0 if keys is None else keys % block
    if tail and causal:
        raise ValueError(
            "causal and a padded key length would need one mask carrying both; "
            "pad Lkv to a whole number of blocks and leave `keys` unset"
        )

    kblocks = ceildiv(k.rows, block)
    #: Lane groups and K-chunks spanned by one block, in the units each addresses.
    ktiles = ceildiv(block, L.LANES * gn)
    kchunks = ceildiv(block, L.KBLOCK * nk)
    #: The query span one pass carries: a block, or every row there is.
    span = qblock if qblock is not None else q.rows
    qtiles = ceildiv(span, L.LANES * gm)

    top = L.temp(span, block)  # running max, broadcast across each row
    total = L.temp(span, block)  # running denominator, likewise
    corr = L.temp(span, block)  # this block's rescale of everything before it
    scores = L.temp(span, block)
    weights = L.temp(span, block)
    part_o = L.temp(span, Dv)
    acc_o = L.temp(span, Dv)

    # Lower triangular, built here because `block` is an ordinary Python value
    # while tracing. Only the block straddling the diagonal needs it.
    tri = L.table(np.tril(np.ones((block, block), np.float16))) if causal else None
    #: A mask is as tall as the query span, which a mask forced to be a block.
    rows = block if qblock is None else qblock
    edge = (
        L.table(np.tile(np.arange(block) < tail, (rows, 1)).astype(np.float16))
        if tail
        else None
    )

    def key_block(base, j, mask):
        """One key block folded into the running softmax, in four stages."""
        with units(qtiles, ktiles) as (a, b):
            sw = L.tile(gm, gn, nk)
            for c in loop(q.chunks32(nk)):
                sw += q[base + a, c] @ k[j * ktiles + b, c]
            scores[a, b] <<= sw

        # ONE band. `c` and `p` stay VALUES: reading `corr` back here instead
        # costs a ninth descriptor and the band is refused.
        with units(1) as e:
            m2 = L.maximum(top[e], L.row_max(scores[e]))
            c = L.exp2(top[e] - m2)
            p = L.exp2(scores[e] - m2)
            corr[e] <<= c
            weights[e] <<= p
            total[e] <<= total[e] * c + L.row_sum(p)
            top[e] <<= m2

        if mask is not None:
            # Zeroing AFTER the exponential is exact: softmax is shift
            # invariant, and a row keeps its diagonal element either way.
            with units(1) as e:
                masked = weights[e] * mask[e]
                weights[e] <<= masked
                # An OVERWRITE, not an accumulation: a masked block is always
                # traced first, so there is no earlier total to carry.
                total[e] <<= L.row_sum(masked)

        with units(qtiles, v.tiles(gn)) as (a, b):
            ow = L.tile(gm, gn, nk)
            for c in loop(kchunks):
                ow += weights[a, c] @ v[b, j * kchunks + c]
            part_o[a, b] <<= ow

        with units(acc_o.parts(part)) as e:
            acc_o[e] <<= acc_o[e] * corr[e] + part_o[e]

    def sweep(base, out, blocks):
        """Every key block against one query span, then the normalising divide."""
        with units(top.parts(part)) as e:
            total[e] <<= 0.0
            acc_o[e] <<= 0.0
            top[e] <<= NEG
        # Associative, so order is free -- but a MASKED block goes first: the
        # loop after it traces ONE body for every block in it.
        if causal:
            key_block(base, blocks, tri)
            for j in loop(blocks):
                key_block(base, j, None)
        elif edge is not None:
            # `kblocks` is symbolic, so no branch can pick out the last block.
            key_block(base, kblocks - 1, edge)
            for j in loop(kblocks - 1):
                key_block(base, j, None)
        else:
            for j in loop(kblocks):
                key_block(base, j, None)
        with units(acc_o.parts(part)) as e:
            out[e] <<= acc_o[e] / total[e]

    if qblock is None:
        # The whole query length at once: `groups` needs no loop, since every
        # query row attends every key of its own KV head.
        sweep(0, o, None)
    else:
        for i in loop(ceildiv(q.rows, qblock)):
            sweep(i * qtiles, o.rows_from(i * qblock), i)
