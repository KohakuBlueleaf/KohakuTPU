"""Experimental FP16 planar attention; score scratch is retained, not flash."""

from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.language import dsl as kt
from kohakutpu.language.examples.dsl.cluster.sweep import core_blocks, sweep


class RowWindow:
    """A row-offset argument to existing bodies, indexed before every DSL op."""

    def __init__(self, base, offset, rows):
        self.base, self.offset = base, offset
        self.shape = (rows, base.shape[1])

    def __getitem__(self, axes):
        rows, columns = axes
        return self.base[self.offset + rows.start : self.offset + rows.stop, columns]


def _product_kernel(heads, sq, tk_total, dim, tq=64, tk=64, valid_keys=None):
    valid_keys = tk_total if valid_keys is None else valid_keys
    ports = {
        "q": kt.In(heads * sq, dim, dtype="mx7a", layout=dict(gm=tq // 4, nk=2)),
        "k": kt.In(heads * tk_total, dim, dtype="mx7b", layout=dict(gn=tk // 4, nk=2)),
        "vt": kt.In(
            heads * dim, tk_total, dtype="mx7b", layout=dict(gn=dim // 4, nk=2)
        ),
        "valid": kt.In(heads * sq, tk, layout=dict(tile=(tq // 4, tk // 4))),
        "o": kt.Out(heads * sq, dim),
    }

    def body(q, k, vt, valid, o):
        srows = heads * sq
        scores = kt.buffer((srows, tk_total), "f16", tile=(tq // 4, tk // 4))
        probs = kt.buffer((srows, tk_total), "mx7a", gm=tq // 4, nk=2)
        nb = tk_total // tk
        maxima = kt.buffer((nb, srows), "f16")
        sums = kt.buffer((nb, srows), "f16")
        rowmax = kt.buffer((srows,), "f16")
        rowsum = kt.buffer((srows,), "f16")
        product = kt.buffer((srows, dim), "f16", tile=(tq // 4, dim // 4))
        scale = dim**-0.5 * kt.LOG2E
        sweep(q, k, scores, tq, tk, groups=heads)
        for b in range(nb):
            need_mask = (b + 1) * tk > valid_keys or valid_keys >= tk_total
            for r0, _, rper in core_blocks(srows, tk, tk, 16, tq):
                for t in kt.range(rper // 16):
                    r = r0 + t * 16
                    x = kt.load(scores[r : r + 16, b * tk : (b + 1) * tk]) * scale
                    if need_mask:
                        mask = kt.load(valid[r : r + 16, 0:tk])
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
            need_mask = (b + 1) * tk > valid_keys or valid_keys >= tk_total
            for r0, _, rper in core_blocks(srows, tk, tk, 16, tq):
                for t in kt.range(rper // 16):
                    r = r0 + t * 16
                    x = kt.load(scores[r : r + 16, b * tk : (b + 1) * tk]) * scale
                    if need_mask:
                        mask = kt.load(valid[r : r + 16, 0:tk])
                        x = kt.where(mask, x, float("-inf"))
                    p = kt.exp2(x - kt.load(rowmax[r : r + 16]))
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
        for r0, _, rper in core_blocks(srows, dim, dim, 16, tq):
            for t in kt.range(rper // 16):
                r = r0 + t * 16
                value = kt.load(product[r : r + 16, :])
                kt.store(
                    o[r : r + 16, :], value * kt.recip(kt.load(rowsum[r : r + 16]))
                )

    return kt.Kernel.build("attention_partial", body, ports)


def kernel(
    heads,
    queries,
    keys,
    dim,
    cores=1,
    query_tile=16,
    key_tile=64,
    *,
    valid_keys=None,
    query_blocked=False,
    block_rows=None,
    key_rows=None,
):
    sq = -(-queries // (cores * query_tile)) * cores * query_tile
    sk = -(-keys // key_tile) * key_tile
    k_rows = keys if key_rows is None else key_rows
    valid_keys = keys if valid_keys is None else valid_keys
    block = (
        (cores * query_tile if block_rows is None else block_rows)
        if query_blocked
        else sq
    )
    mask_rows = block if query_blocked else heads * sq
    product = _product_kernel(
        1 if query_blocked else heads,
        block,
        sk,
        dim,
        query_tile,
        key_tile,
        valid_keys=valid_keys,
    )
    qpack, _ = MM.quant_kernel(
        Operand("full", pitch=dim, rows=heads * queries),
        queries,
        dim,
        sq,
        dim,
        "a",
        query_tile // 4,
        2,
        heads,
    )
    kpack, _ = MM.quant_kernel(
        Operand("full", pitch=dim, rows=heads * k_rows),
        k_rows,
        dim,
        sk,
        dim,
        "b",
        key_tile // 4,
        2,
        heads,
    )
    vpack, _ = MM.quant_kernel(
        Operand("full", pitch=keys, rows=heads * dim),
        dim,
        keys,
        dim,
        sk,
        "b",
        dim // 4,
        2,
        heads,
    )
    ports = {
        "q": kt.In(heads * queries, dim),
        "k": kt.In(heads * k_rows, dim),
        "vt": kt.In(heads * dim, keys),
        "out": kt.Out(heads * queries, dim),
    }

    if valid_keys % 16:
        ports["tail"] = kt.In(16)

    def body(q, k, vt, out, tail=None):
        qp = kt.buffer((heads * sq, dim), "mx7a", gm=query_tile // 4, nk=2)
        kp = kt.buffer((heads * sk, dim), "mx7b", gn=key_tile // 4, nk=2)
        vp = kt.buffer((heads * dim, sk), "mx7b", gn=dim // 4, nk=2)
        qpack.fn(x=q, q=qp)
        kpack.fn(x=k, q=kp)
        vpack.fn(x=vt, q=vp)
        nb = sk // key_tile
        b_tail = min(valid_keys // key_tile, nb - 1)
        rem_keys = max(0, min(key_tile, valid_keys - b_tail * key_tile))
        full = (rem_keys // 16) * 16
        mask = kt.buffer((1, key_tile), "f16")
        kt.fill_value(mask, 0)
        if full:
            kt.fill_value(mask[:, :full], 0x3C00, ew=16)
        if valid_keys % 16:
            kt.copy(
                mask,
                tail,
                dat=full * 2,
                walk=((1, 32),),
                dwalk=((1, 32),),
            )
        tiled_mask = kt.buffer(
            (mask_rows, key_tile), "f16", tile=(query_tile // 4, key_tile // 4)
        )
        kt.gt4(
            tiled_mask,
            mask,
            walk=(
                (mask_rows // query_tile, 0),
                (query_tile // 4, 0),
                (key_tile // 16, 32),
                (4, 0),
            ),
            dwalk=(
                (mask_rows // query_tile, query_tile * key_tile * 2),
                (query_tile // 4, key_tile * 8),
                (key_tile // 16, 128),
            ),
        )
        result = out if queries == sq else kt.buffer((heads * sq, dim), "f16")
        if query_blocked:
            for head in kt.range(heads):
                for qi in kt.range(sq // block):
                    r = head * sq + qi * block
                    product.fn(
                        q=RowWindow(qp, r, block),
                        k=RowWindow(kp, head * sk, sk),
                        vt=RowWindow(vp, head * dim, dim),
                        valid=tiled_mask,
                        o=RowWindow(result, r, block),
                    )
        else:
            product.fn(q=qp, k=kp, vt=vp, valid=tiled_mask, o=result)
        if queries != sq:
            kt.copy(
                out,
                result,
                walk=((heads, sq * dim * 2), (queries * dim // 16, 32)),
                dwalk=((heads, queries * dim * 2), (queries * dim // 16, 32)),
                classed=True,
                node_loop=True,
            )

    return kt.Kernel.build("native_attention", body, ports)
