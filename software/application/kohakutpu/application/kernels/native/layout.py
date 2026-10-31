"""Projection-layout Q/K/V extraction and inverse head concatenation."""

from kohakutpu.application.kernels import transpose as T
from kohakutpu.application.kernels.native import attention as A
from kohakutpu.application.kernels.native.composition import (
    GraphKernel,
    apply_quant_backend,
    renamed,
    sequence,
)
from kohakutpu.language import dsl as kt
from kohakutpu.language.examples.dsl.cluster.sweep import core_blocks, sweep
from kohakutpu.stack.kernel import dsl_machine


def split_kernel(
    batch,
    tokens,
    heads,
    dim,
    cores,
    *,
    query_pad=None,
    key_pad=None,
    batch_offset=0,
    total_batch=None,
    head_offset=0,
    total_heads=None,
):
    """Token-major logical rows to zero-padded planar Q/K and transposed V."""
    if dim % 16:
        raise ValueError("projection head width must contain whole 16-element words")
    total_batch = batch if total_batch is None else total_batch
    total_heads = heads if total_heads is None else total_heads
    padded = -(-tokens // 16) * 16 if query_pad is None else query_pad
    k_padded = (-(-tokens // 16) * 16) if key_pad is None else key_pad
    width = total_heads * dim
    planes = batch * heads
    words = planes * k_padded * dim // 16
    ports = {
        "x": kt.In(total_batch * tokens, 3 * width),
        "mask": kt.In(2, 128),
        "q": kt.Out(planes * padded, dim),
        "k": kt.Out(planes * k_padded, dim),
        "vt": kt.Out(planes * dim, k_padded),
    }

    def body(x, mask, q, k, vt):
        v = kt.buffer((words, 16), "f16")
        for i, (dst, rows) in enumerate(((q, padded), (k, k_padded), (v, k_padded))):
            kt.fill_value(dst, 0)
            kt.copy(
                dst,
                x,
                at=(batch_offset * tokens * 3 * width + i * width + head_offset * dim)
                * 2,
                walk=(
                    (batch, tokens * 3 * width * 2),
                    (heads, dim * 2),
                    (tokens, 3 * width * 2),
                    (dim // 16, 32),
                ),
                dwalk=((planes, rows * dim * 2), (tokens * dim // 16, 32)),
            )
        T.emit(v, mask, 0, vt, planes, k_padded, dim, cores)

    return kt.Kernel.build("native_split_projection", body, ports)


def merge_kernel(
    batch,
    tokens,
    heads,
    dim,
    *,
    query_pad=None,
    batch_offset=0,
    total_batch=None,
    head_offset=0,
    total_heads=None,
):
    total_batch = batch if total_batch is None else total_batch
    total_heads = heads if total_heads is None else total_heads
    full_width = total_heads * dim
    padded = -(-tokens // 16) * 16 if query_pad is None else query_pad
    words = batch * tokens * heads * dim // 16
    ports = {
        "x": kt.In(batch * heads * padded, dim),
        "out": kt.Out(total_batch * tokens, full_width),
    }

    def body(x, out):
        dwalk = (
            ((words, 32),)
            if heads == total_heads
            else ((batch * tokens, full_width * 2), (heads * dim // 16, 32))
        )
        kt.copy(
            out,
            x,
            dat=(batch_offset * tokens * full_width + head_offset * dim) * 2,
            walk=(
                (batch, heads * padded * dim * 2),
                (tokens, dim * 2),
                (heads, padded * dim * 2),
                (dim // 16, 32),
            ),
            dwalk=dwalk,
        )

    return kt.Kernel.build("native_merge_heads", body, ports)


def _chunk_attention(
    batch,
    tokens,
    heads,
    dim,
    cores,
    *,
    query_blocked=False,
    block_rows=None,
    query_tile=16,
    query_pad=None,
    batch_offset=0,
    total_batch=None,
    head_offset=0,
    total_heads=None,
):
    padded = -(-tokens // 16) * 16 if query_pad is None else query_pad
    sk = -(-padded // 64) * 64
    split = split_kernel(
        batch,
        tokens,
        heads,
        dim,
        cores,
        query_pad=padded,
        key_pad=sk,
        batch_offset=batch_offset,
        total_batch=total_batch,
        head_offset=head_offset,
        total_heads=total_heads,
    )
    attn = A.kernel(
        batch * heads,
        padded,
        sk,
        dim,
        cores,
        query_tile=query_tile,
        valid_keys=tokens,
        query_blocked=query_blocked,
        block_rows=block_rows,
    )
    merge = merge_kernel(
        batch,
        tokens,
        heads,
        dim,
        query_pad=padded,
        batch_offset=batch_offset,
        total_batch=total_batch,
        head_offset=head_offset,
        total_heads=total_heads,
    )
    ports = {
        "x": split.ports["x"],
        "mask": split.ports["mask"],
        "out": merge.ports["out"],
    }
    if tokens % 16:
        ports["tail"] = kt.In(16)

    def body(x, mask, out, tail=None):
        q = kt.buffer((batch * heads * padded, dim), "f16")
        k = kt.buffer((batch * heads * sk, dim), "f16")
        vt = kt.buffer((batch * heads * dim, sk), "f16")
        split.fn(x=x, mask=mask, q=q, k=k, vt=vt)
        attn.fn(q=q, k=k, vt=vt, out=q, tail=tail)
        merge.fn(x=q, out=out)

    return kt.Kernel.build("native_projection_attention", body, ports)


def _fast_chunk_attention(
    batch,
    tokens,
    heads,
    dim,
    cores,
    *,
    q_pad,
    sk,
    q_tile,
    key_tile,
    step_heads,
    batch_offset=0,
    total_batch=None,
    head_offset=0,
    total_heads=None,
    init_valid=True,
    mover_overlap=True,
):
    total_batch = batch if total_batch is None else total_batch
    total_heads = heads if total_heads is None else total_heads
    width = total_heads * dim
    planes = batch * heads
    words = planes * sk * dim // 16
    srows = step_heads * q_pad
    ports = {
        "x": kt.In(total_batch * tokens, 3 * width),
        "mask": kt.In(2, 128),
        "out": kt.Out(total_batch * tokens, width),
    }
    if tokens % 16:
        ports["tail"] = kt.In(16)

    def body(x, mask, out, tail=None):
        q = kt.buffer((planes * sk, dim), "f16")
        v = kt.buffer((planes * dim, sk), "f16")
        s = kt.buffer((words // 8, 128), "f16")
        y_out = kt.buffer((srows, dim), "f16") if mover_overlap else None
        qp = kt.buffer((planes * q_pad, dim), "mx7a", gm=q_tile // 4, nk=2)
        kp = kt.buffer((planes * sk, dim), "mx7b", gn=sk // 4, nk=2)
        vp = kt.buffer((planes * dim, sk), "mx7b", gn=dim // 4, nk=2)
        mask_row = kt.buffer((1, key_tile), "f16")
        valid = kt.buffer((4, key_tile), "f16", tile=(1, key_tile // 4))
        scores = kt.buffer((srows, sk), "f16", tile=(q_tile // 4, sk // 4))
        probs = kt.buffer((srows, sk), "mx7a", gm=q_tile // 4, nk=2)
        sums = kt.buffer((4, srows), "f16")
        rowmax = kt.buffer((srows,), "f16")
        rowsum = kt.buffer((srows,), "f16")
        product = kt.buffer((srows, dim), "f16", tile=(q_tile // 4, dim // 4))

        P, rg, cw, sub = 2 * dim, sk // 4, dim // 16, dim // 4
        trips = (words // 8) // T.ROWS
        base_at = batch_offset * tokens * 3 * width + head_offset * dim

        def emit_v_in():
            kt.fill_value(v, 0)
            kt.copy(
                v,
                x,
                at=(base_at + 2 * width) * 2,
                walk=(
                    (batch, tokens * 3 * width * 2),
                    (heads, dim * 2),
                    (tokens, 3 * width * 2),
                    (dim // 16, 32),
                ),
                dwalk=((planes, sk * dim * 2), (tokens * dim // 16, 32)),
            )
            kt.gt4(
                s,
                v,
                walk=[(planes, sk * P), (rg, 4 * P), (cw, 32), (4, P)],
                dwalk=[(planes, rg * cw * 128), (rg, cw * 128), (cw, 128)],
            )

        def emit_v_transpose():
            for c in kt.par(cores, on=kt.vc):
                m1 = kt.unpack(kt.load(mask[0:1, :]))
                m2 = kt.unpack(kt.load(mask[1:2, :]))
                for k_it in kt.range(trips // cores):
                    r = (c * (trips // cores) + k_it) * T.ROWS
                    val = kt.unpack(kt.load(s[r : r + T.ROWS, :]))
                    a = kt.where(m1, kt.xlane(val, 5), val)
                    kt.store(s[r : r + T.ROWS, :], kt.where(m2, kt.xlane(a, 10), a))

        def emit_v_out():
            kt.gt4(
                q,
                s,
                walk=[
                    (planes, rg * sub * 32),
                    (sub, 32),
                    (sk // 16, 4 * sub * 32),
                    (4, sub * 32),
                ],
                dwalk=[
                    (planes, sub * (sk // 16) * 128),
                    (sub, (sk // 16) * 128),
                    (sk // 16, 128),
                ],
            )
            kt.copy(
                v,
                q,
                walk=[
                    (planes, sub * (sk // 16) * 128),
                    (sub, (sk // 16) * 128),
                    (4, 32),
                    (sk // 16, 128),
                ],
                dwalk=[(words, 32)],
            )
            for b_v in range(planes):
                kt.quant(
                    vp[b_v * dim : (b_v + 1) * dim, :],
                    v[b_v * dim : (b_v + 1) * dim, :],
                )

        def emit_qk_in():
            kt.fill_value(q[: planes * q_pad, :], 0)
            kt.copy(
                q,
                x,
                at=base_at * 2,
                walk=(
                    (batch, tokens * 3 * width * 2),
                    (heads, dim * 2),
                    (tokens, 3 * width * 2),
                    (dim // 16, 32),
                ),
                dwalk=((planes, q_pad * dim * 2), (tokens * dim // 16, 32)),
            )
            kt.quant(qp, q[: planes * q_pad, :])
            kt.fill_value(q, 0)
            kt.copy(
                q,
                x,
                at=(base_at + width) * 2,
                walk=(
                    (batch, tokens * 3 * width * 2),
                    (heads, dim * 2),
                    (tokens, 3 * width * 2),
                    (dim // 16, 32),
                ),
                dwalk=((planes, sk * dim * 2), (tokens * dim // 16, 32)),
            )
            kt.quant(kp, q)
            if init_valid:
                rem_keys = max(0, min(key_tile, tokens - key_tile))
                full = (rem_keys // 16) * 16
                kt.fill_value(mask_row, 0)
                if full:
                    kt.fill_value(mask_row[:, :full], 0x3C00, ew=16)
                if tokens % 16:
                    kt.copy(
                        mask_row,
                        tail,
                        dat=full * 2,
                        walk=((1, 32),),
                        dwalk=((1, 32),),
                    )
                kt.gt4(
                    valid,
                    mask_row,
                    walk=((key_tile // 16, 32), (4, 0)),
                    dwalk=((key_tile // 16, 128),),
                )

        emit_v_in()
        emit_v_transpose()
        if mover_overlap:
            emit_qk_in()
        else:
            emit_v_out()
            emit_qk_in()

        scale = dim**-0.5 * kt.LOG2E
        for hp in range(planes // step_heads):
            r_q = hp * srows
            r_k = hp * step_heads * sk
            r_v = hp * step_heads * dim
            sweep(
                A.RowWindow(qp, r_q, srows),
                A.RowWindow(kp, r_k, step_heads * sk),
                scores,
                q_tile,
                sk,
                groups=step_heads,
            )
            if mover_overlap and hp == 0:
                emit_v_out()
            for r0, _, rper in core_blocks(srows, key_tile, key_tile, 16, q_tile):
                for t in kt.range(rper // 16):
                    r = r0 + t * 16
                    x0 = kt.load(scores[r : r + 16, 0:key_tile]) * scale
                    kt.store(rowmax[r : r + 16], kt.rmax(x0))
            for r0, _, rper in core_blocks(srows, key_tile, key_tile, 16, q_tile):
                vmask = kt.unpack(kt.load(valid[0:4, 0:key_tile]))
                for t in kt.range(rper // 16):
                    r = r0 + t * 16
                    x1 = kt.load(scores[r : r + 16, key_tile : 2 * key_tile]) * scale
                    x1 = kt.where(vmask, x1, float("-inf"))
                    kt.store(
                        rowmax[r : r + 16],
                        kt.maximum(kt.load(rowmax[r : r + 16]), kt.rmax(x1)),
                    )
            for r0, _, rper in core_blocks(srows, key_tile, key_tile, 16, q_tile):
                for t in kt.range(rper // 16):
                    r = r0 + t * 16
                    rm = kt.load(rowmax[r : r + 16])
                    x0 = kt.load(scores[r : r + 16, 0:key_tile]) * scale
                    p0 = kt.exp2(x0 - rm)
                    kt.store(probs[r : r + 16, 0:key_tile], p0)
                    kt.store(sums[0, r : r + 16], kt.rsum(p0[:, 0:64]))
                    kt.store(sums[1, r : r + 16], kt.rsum(p0[:, 64:128]))
            for r0, _, rper in core_blocks(srows, key_tile, key_tile, 16, q_tile):
                vmask = kt.unpack(kt.load(valid[0:4, 0:key_tile]))
                for t in kt.range(rper // 16):
                    r = r0 + t * 16
                    rm = kt.load(rowmax[r : r + 16])
                    x1 = kt.load(scores[r : r + 16, key_tile : 2 * key_tile]) * scale
                    x1 = kt.where(vmask, x1, float("-inf"))
                    p1 = kt.exp2(x1 - rm)
                    kt.store(probs[r : r + 16, key_tile : 2 * key_tile], p1)
                    kt.store(sums[2, r : r + 16], kt.rsum(p1[:, 0:64]))
                    kt.store(sums[3, r : r + 16], kt.rsum(p1[:, 64:128]))
            sweep(
                probs,
                A.RowWindow(vp, r_v, step_heads * dim),
                product,
                q_tile,
                dim,
                groups=step_heads,
            )
            for r0, _, rper in core_blocks(srows, 32, 32, 16, q_tile):
                for t in kt.range(rper // 16):
                    r = r0 + t * 16
                    z = kt.unpack(kt.load(sums[0, r : r + 16]))
                    for b in range(1, 4):
                        z = z + kt.load(sums[b, r : r + 16])
                    kt.store(rowsum[r : r + 16], z)
            for r0, _, rper in core_blocks(srows, dim, dim, 16, q_tile):
                for t in kt.range(rper // 16):
                    r = r0 + t * 16
                    value = kt.load(product[r : r + 16, :])
                    dst_slice = (
                        y_out[r : r + 16, :]
                        if mover_overlap
                        else q[r_q + r : r_q + r + 16, :]
                    )
                    kt.store(
                        dst_slice,
                        value * kt.recip(kt.load(rowsum[r : r + 16])),
                    )
            if mover_overlap:
                kt.copy(
                    out,
                    y_out,
                    dat=(base_at + hp * step_heads * dim) * 2,
                    walk=(
                        (batch, step_heads * q_pad * dim * 2),
                        (tokens, dim * 2),
                        (step_heads, q_pad * dim * 2),
                        (dim // 16, 32),
                    ),
                    dwalk=(
                        (total_batch * tokens, width * 2),
                        (step_heads * dim // 16, 32),
                    ),
                )

        if not mover_overlap:
            dwalk = (
                ((batch * tokens * heads * dim // 16, 32),)
                if heads == total_heads
                else ((total_batch * tokens, width * 2), (heads * dim // 16, 32))
            )
            kt.copy(
                out,
                q,
                dat=base_at * 2,
                walk=(
                    (batch, heads * q_pad * dim * 2),
                    (tokens, dim * 2),
                    (heads, q_pad * dim * 2),
                    (dim // 16, 32),
                ),
                dwalk=dwalk,
            )

    return kt.Kernel.build("native_fast_projection_attention", body, ports)


def attention_kernel(
    batch,
    tokens,
    heads,
    dim,
    cores,
    *,
    query_blocked=False,
    space="dram",
    machine=None,
    block_rows=None,
    fast_attn=True,
    mover_overlap=True,
    quant_backend="mover_quant",
):
    if space == "stage" and machine is not None:
        given = {"x": (batch * tokens, 3 * heads * dim), "mask": (2, 128)}
        if tokens % 16:
            given["tail"] = (16,)
        if fast_attn and cores >= 2 and tokens > 64 and dim == 64:
            q_pad = -(-tokens // 16) * 16
            sk = -(-q_pad // 64) * 64
            q_tile = min(q_pad, 256)
            if sk == 256 and (q_tile // 4) * (sk // 4) <= 4096 and heads % cores == 0:
                key_tile = 128
                step_heads = min(cores, heads)
                chunk_h = (
                    4
                    if cores == 2 and heads % 4 == 0
                    else (3 if cores == 1 and heads % 3 == 0 else step_heads)
                )
                chunks = []
                for b0 in range(batch):
                    for h0 in range(0, heads, chunk_h):
                        k = _fast_chunk_attention(
                            1,
                            tokens,
                            chunk_h,
                            dim,
                            cores,
                            q_pad=q_pad,
                            sk=sk,
                            q_tile=q_tile,
                            key_tile=key_tile,
                            step_heads=step_heads,
                            batch_offset=b0,
                            total_batch=batch,
                            head_offset=h0,
                            total_heads=heads,
                            init_valid=(b0 == 0 and h0 == 0),
                            mover_overlap=mover_overlap,
                        )
                        chunks.append(
                            k.trace(given, machine=dsl_machine(machine)).graph
                        )
                if mover_overlap and len(chunks) > 1:
                    for i in range(len(chunks) - 1):
                        idx_merge0 = next(
                            j
                            for j, st in enumerate(chunks[i]["body"])
                            if st.get("kind") == "mover"
                            and st.get("op") == "copy"
                            and st.get("dst", {}).get("buffer") == "out"
                        )
                        idx_pv0 = idx_merge0 - 3
                        idx_qkt1 = idx_merge0 + 1
                        idx_pv1 = idx_qkt1 + 5
                        chunks[i]["body"] = (
                            chunks[i]["body"][:idx_pv0]
                            + chunks[i + 1]["body"][:3]
                            + chunks[i]["body"][idx_pv0 : idx_qkt1 + 2]
                            + chunks[i + 1]["body"][4:10]
                            + chunks[i]["body"][idx_qkt1 + 2 : idx_pv1 + 1]
                            + [chunks[i + 1]["body"][3]]
                            + chunks[i]["body"][idx_pv1 + 1 :]
                        )
                        chunks[i + 1]["body"] = chunks[i + 1]["body"][10:]
                seq = sequence(chunks, space="stage")
                seq = apply_quant_backend(seq, quant_backend, machine)
                return GraphKernel(seq)
        q_pad = -(-tokens // (cores * 16)) * cores * 16
        sk = -(-q_pad // 64) * 64
        q_tile = min(q_pad // cores, 256)
        eff_block = (
            (q_pad if query_blocked else None) if block_rows is None else block_rows
        )
        plane_bytes = q_pad * dim * 3 + sk * dim * 12
        attn_bytes = q_pad * (sk * 3 + 128) + q_pad * dim * 2 + sk * 16 + q_pad * 20
        unblocked_cap = 950000 // (plane_bytes + attn_bytes)
        if unblocked_cap >= heads:
            eff_qb = False
            max_b = max(1, unblocked_cap // heads)
            chunk_b = max(b for b in range(1, min(batch, max_b) + 1) if batch % b == 0)
            chunk_h = heads
        else:
            eff_qb = query_blocked
            max_h = max(1, (1040000 - attn_bytes) // plane_bytes)
            chunk_b = 1
            chunk_h = (
                next(h for h in range(min(heads, max_h), 0, -1) if heads % h == 0)
                if heads > max_h
                else heads
            )
        chunks = []
        for b0 in range(0, batch, chunk_b):
            for h0 in range(0, heads, chunk_h):
                k = _chunk_attention(
                    chunk_b,
                    tokens,
                    chunk_h,
                    dim,
                    cores,
                    query_blocked=eff_qb,
                    block_rows=eff_block,
                    query_tile=q_tile,
                    query_pad=q_pad,
                    batch_offset=b0,
                    total_batch=batch,
                    head_offset=h0,
                    total_heads=heads,
                )
                traced = k.trace(given, machine=dsl_machine(machine)).graph
                traced = dict(
                    traced,
                    scratch=[s for s in traced["scratch"] if s["name"] != "h7"],
                    body=renamed(traced["body"], {"h7": "h4"}),
                )
                chunks.append(traced)
        seq = sequence(chunks, space="stage")
        seq = apply_quant_backend(seq, quant_backend, machine)
        return GraphKernel(seq)
    return _chunk_attention(
        batch,
        tokens,
        heads,
        dim,
        cores,
        query_blocked=query_blocked,
        block_rows=block_rows,
    )
