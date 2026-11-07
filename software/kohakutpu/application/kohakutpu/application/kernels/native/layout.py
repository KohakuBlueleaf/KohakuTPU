"""Projection-layout Q/K/V extraction and inverse head concatenation."""

from kohakutpu.application.kernels import transpose as T
from kohakutpu.application.kernels.native import attention as A
from kohakutpu.application.kernels.template import GraphKernel, TemplateCall
from kohakutpu.application.runtime.generated import template_graph as graph
from kohakutpu.language import dsl as kt
from kohakutpu.language.config import ENV


def split_call(batch, tokens, heads, dim, *, space="dram"):
    """Typed strided planes with padded tokens and an exact masked V transpose."""
    width = heads * dim
    padded = -(-tokens // 16) * 16
    planes = batch * heads
    return TemplateCall(
        "fused.kernel",
        {
            "ports": [
                {
                    "name": "x",
                    "shape": [batch * tokens, 3 * width],
                    "dtype": "f16",
                    "written": False,
                },
                {"name": "mask", "shape": [2, 128], "dtype": "f16", "written": False},
                {
                    "name": "q",
                    "shape": [planes * padded, dim],
                    "dtype": "f16",
                    "written": True,
                },
                {
                    "name": "k",
                    "shape": [planes * padded, dim],
                    "dtype": "f16",
                    "written": True,
                },
                {
                    "name": "vt",
                    "shape": [planes * dim, padded],
                    "dtype": "f16",
                    "written": True,
                },
            ],
            "scratch": [],
            "staged": [],
            "scratch_space": space,
            "steps": [
                {
                    "kind": "planes",
                    "planes": {
                        "src": "x",
                        "dst": output,
                        "mask": "mask" if output == "vt" else None,
                        "batches": batch,
                        "groups": heads,
                        "rows": tokens,
                        "columns": dim,
                        "padded_rows": padded,
                        "source_offset": index * width,
                        "batch_stride": tokens * 3 * width,
                        "group_stride": dim,
                        "row_stride": 3 * width,
                    },
                }
                for index, output in enumerate(("q", "k", "vt"))
            ],
        },
    )


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


def chunk_attention_call(
    batch,
    tokens,
    heads,
    dim,
    *,
    query_blocked,
    block_rows,
    query_tile,
    query_pad,
    query_cores,
    key_pad=None,
    batch_offset=0,
    total_batch=None,
    head_offset=0,
    total_heads=None,
):
    """Retained named stages; Q is reused only after all Q packing is complete."""
    total_batch = batch if total_batch is None else total_batch
    total_heads = heads if total_heads is None else total_heads
    width = total_heads * dim
    keys = -(-query_pad // 64) * 64 if key_pad is None else key_pad
    planes = batch * heads
    ports = [
        {
            "name": "x",
            "shape": [total_batch * tokens, 3 * width],
            "dtype": "f16",
            "written": False,
        },
        {"name": "mask", "shape": [2, 128], "dtype": "f16", "written": False},
        {
            "name": "out",
            "shape": [total_batch * tokens, width],
            "dtype": "f16",
            "written": True,
        },
    ]
    if tokens % 16:
        ports.append({"name": "tail", "shape": [16], "dtype": "f16", "written": False})
    steps = []
    for index, (output, padded) in enumerate(
        (("q", query_pad), ("k", keys), ("vt", keys))
    ):
        steps.append(
            {
                "kind": "planes",
                "planes": {
                    "src": "x",
                    "dst": output,
                    "mask": "mask" if output == "vt" else None,
                    "batches": batch,
                    "groups": heads,
                    "rows": tokens,
                    "columns": dim,
                    "padded_rows": padded,
                    "source_offset": batch_offset * tokens * 3 * width
                    + head_offset * dim
                    + index * width,
                    "batch_stride": tokens * 3 * width,
                    "group_stride": dim,
                    "row_stride": 3 * width,
                },
            }
        )
    steps.append(
        {
            "kind": "attention",
            "attention": {
                "q": "q",
                "k": "k",
                "vt": "vt",
                "out": "q",
                "tail": "tail" if tokens % 16 else None,
                "heads": planes,
                "queries": query_pad,
                "keys": keys,
                "dim": dim,
                "query_tile": query_tile,
                "query_cores": query_cores,
                "key_tile": 64,
                "valid_keys": tokens,
                "schedule": {
                    "finite_batch": int(ENV.get("KTPU_PIPE_FINITE_BATCH", "4")),
                    **{
                        key: int(ENV.get(env, default) == "1")
                        for key, env, default in (
                            ("overlap", "KTPU_PIPE_OVERLAP", "1"),
                            (
                                "classifier_overlap",
                                "KTPU_QUANT_CLASSIFIER_OVERLAP",
                                "0",
                            ),
                            ("pipeline", "KTPU_QUANT_PIPELINE", "1"),
                            ("zero_classifier", "KTPU_QUANT_ZERO_CLASSIFIER", "1"),
                            ("stream_axes", "KTPU_QUANT_STREAM_AXES", "1"),
                            ("fill_overlap", "KTPU_QUANT_FILL_OVERLAP", "1"),
                            ("zero_reuse", "KTPU_QUANT_ZERO_REUSE", "1"),
                        )
                    },
                },
                "blocked": query_blocked,
                "block_rows": (
                    (block_rows if block_rows is not None else query_pad)
                    if query_blocked
                    else query_pad
                ),
            },
        }
    )
    steps.append(
        {
            "kind": "planes",
            "planes": {
                "scatter": True,
                "src": "q",
                "dst": "out",
                "mask": None,
                "batches": batch,
                "groups": heads,
                "rows": tokens,
                "columns": dim,
                "padded_rows": query_pad,
                "source_offset": batch_offset * tokens * width + head_offset * dim,
                "batch_stride": tokens * width,
                "group_stride": dim,
                "row_stride": width,
            },
        }
    )
    return TemplateCall(
        "fused.kernel",
        {
            "ports": ports,
            "scratch": [
                {"name": "q", "shape": [planes * query_pad, dim], "dtype": "f16"},
                {"name": "k", "shape": [planes * keys, dim], "dtype": "f16"},
                {"name": "vt", "shape": [planes * dim, keys], "dtype": "f16"},
            ],
            "staged": [],
            "scratch_space": "stage",
            "steps": steps,
            "group_ends": [len(steps)],
        },
    )


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


def fast_attention_call(
    batch, tokens, heads, dim, cores, *, chunk_heads, mover_overlap, quant_backend
):
    """Ordered typed fast-policy chunks; shared lowering owns storage and motion."""
    padded = -(-tokens // 16) * 16
    arguments = None
    group_ends = []
    for b0 in range(batch):
        for h0 in range(0, heads, chunk_heads):
            call = chunk_attention_call(
                1,
                tokens,
                chunk_heads,
                dim,
                query_blocked=False,
                block_rows=padded,
                query_tile=padded,
                query_pad=padded,
                query_cores=1,
                key_pad=256,
                batch_offset=b0,
                total_batch=batch,
                head_offset=h0,
                total_heads=heads,
            )
            stages = call.arguments["steps"]
            policy = stages[3]["attention"]
            policy.update(
                key_tile=128,
                score_tile=256,
                head_group=min(cores, heads),
                incremental_max=True,
                sum_columns=64,
                compact_mask=True,
                direct_quant=True,
                schedule={},
            )
            if mover_overlap:
                policy["merge"] = stages.pop()["planes"]
            # V extraction precedes Q/K, as in the fast schedule. The common
            # dependency scheduler, not positional body slices, may overlap it.
            stages[:3] = [stages[2], stages[0], stages[1]]
            if arguments is None:
                arguments = call.arguments
            else:
                arguments["steps"].extend(stages)
            group_ends.append(len(arguments["steps"]))
    if arguments is None:
        raise ValueError("fast projection attention needs a head group")
    arguments.update(
        group_ends=group_ends,
        quant_backend=quant_backend,
        pipeline_lookahead=mover_overlap,
    )
    return TemplateCall("fused.kernel", arguments)


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
        if fast_attn and cores >= 2 and tokens > 64 and dim == 64:
            q_pad = -(-tokens // 16) * 16
            sk = -(-q_pad // 64) * 64
            q_tile = min(q_pad, 256)
            if sk == 256 and (q_tile // 4) * (sk // 4) <= 4096 and heads % cores == 0:
                chunk_h = 4 if cores == 2 and heads % 4 == 0 else min(cores, heads)
                call = fast_attention_call(
                    batch,
                    tokens,
                    heads,
                    dim,
                    cores,
                    chunk_heads=chunk_h,
                    mover_overlap=mover_overlap,
                    quant_backend=quant_backend,
                )
                return GraphKernel(
                    graph(call.generator, call.arguments, machine), template_call=call
                )
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
                call = chunk_attention_call(
                    chunk_b,
                    tokens,
                    chunk_h,
                    dim,
                    query_blocked=eff_qb,
                    block_rows=eff_block,
                    query_tile=q_tile,
                    query_pad=q_pad,
                    query_cores=cores,
                    batch_offset=b0,
                    total_batch=batch,
                    head_offset=h0,
                    total_heads=heads,
                )
                chunks.append({"gen": call.generator, "args": call.arguments})
        call = TemplateCall(
            "kernel.sequence",
            {
                "members": chunks,
                "quant_backend": quant_backend,
            },
        )
        return GraphKernel(
            graph(call.generator, call.arguments, machine), template_call=call
        )
    return _chunk_attention(
        batch,
        tokens,
        heads,
        dim,
        cores,
        query_blocked=query_blocked,
        block_rows=block_rows,
    )
