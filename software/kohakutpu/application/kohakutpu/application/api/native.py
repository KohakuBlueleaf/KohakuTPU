"""Eager fused FP16 operators; the unfused expression path remains selectable.

Use a Device with library_only=True for compile-free execution. Library misses
are errors from that device, never requests to compile in this adapter.
"""

import numpy as np

from kohakutpu.application.api.attention import attention as unfused_attention
from kohakutpu.application.api.custom import call
from kohakutpu.application.api.tensor import tensor
from kohakutpu.application.kernels.native import attention as A
from kohakutpu.application.kernels.native import conv as C
from kohakutpu.application.kernels.native import generated as G
from kohakutpu.application.kernels.native import layout as Y
from kohakutpu.application.kernels.native import linear as L
from kohakutpu.application.kernels.native import rows as R
from kohakutpu.application.runtime import device as D
from kohakutpu.stack import rs


def qkv_projection(
    x, weight, bias, mask, *, heads=None, dim=64, fused=True, ring_depth=2
):
    """Project [B,T,W] and write planar Q, K and transposed V directly."""
    batch, tokens, width = x.shape
    heads = width // dim if heads is None else heads
    if not fused:
        proj = linear(x, weight, bias, fused=False)
        return call(
            Y.split_kernel(batch, tokens, heads, dim, D.device().machine.count("VC")),
            proj.reshape(batch * tokens, 3 * width),
            mask,
        )
    k = G.qkv_projection(
        batch,
        tokens,
        width,
        D.device().machine,
        ring_depth=ring_depth,
        heads=heads,
        dim=dim,
    )
    return call(k, x.reshape(batch * tokens, width), weight, bias, mask)


def norm_qkv(
    x,
    scale,
    shift,
    weight,
    bias,
    mask,
    *,
    eps=1e-5,
    heads=None,
    dim=64,
    fused=True,
    ring_depth=2,
):
    """LayerNorm + QKV projection writing planar Q, K and transposed V directly."""
    batch, tokens, width = x.shape
    heads = width // dim if heads is None else heads
    if not fused:
        proj = norm_linear(x, scale, shift, weight, bias, eps=eps, fused=False)
        return call(
            Y.split_kernel(batch, tokens, heads, dim, D.device().machine.count("VC")),
            proj.reshape(batch * tokens, 3 * width),
            mask,
        )
    k = G.qkv_projection(
        batch,
        tokens,
        width,
        D.device().machine,
        eps=eps,
        ring_depth=ring_depth,
        heads=heads,
        dim=dim,
    )
    return call(k, x.reshape(batch * tokens, width), scale, shift, weight, bias, mask)


def projection_attention(
    x,
    mask,
    *,
    heads,
    dim,
    fused=True,
    query_blocked=False,
    space="dram",
    fast_attn=True,
    mover_overlap=True,
    quant_backend="mover_quant",
):
    """Consume [B,T,3*H*D] directly; output is token-major [B,T,H*D]."""
    batch, tokens, _ = x.shape
    machine = D.device().machine
    cores = machine.count("VC")
    tail = (
        [tensor((np.arange(16) < tokens % 16).astype(np.float16))]
        if tokens % 16
        else []
    )
    if fused:
        return call(
            Y.attention_kernel(
                batch,
                tokens,
                heads,
                dim,
                cores,
                query_blocked=query_blocked,
                space=space,
                machine=machine,
                fast_attn=fast_attn,
                mover_overlap=mover_overlap,
                quant_backend=quant_backend,
            ),
            x.reshape(batch * tokens, 3 * heads * dim),
            mask,
            *tail,
        )[0].reshape(batch, tokens, heads * dim)
    q, k, vt = call(
        Y.split_kernel(batch, tokens, heads, dim, cores),
        x.reshape(batch * tokens, 3 * heads * dim),
        mask,
    )
    padded = -(-tokens // 16) * 16
    if tokens != padded:
        y = call(
            A.kernel(batch * heads, padded, padded, dim, cores, valid_keys=tokens),
            q,
            k,
            vt,
            *tail,
        )[0]
    else:
        y = attention(
            q.reshape(batch * heads, tokens, dim),
            k.reshape(batch * heads, tokens, dim),
            vt.reshape(batch * heads, dim, tokens),
            fused=False,
        )
    return call(
        Y.merge_kernel(batch, tokens, heads, dim),
        y.reshape(batch * heads * padded, dim),
    )[0].reshape(batch, tokens, heads * dim)


def conv2d(
    x,
    weight,
    bias,
    *,
    stride=1,
    padding=0,
    activation="none",
    residual=None,
    fused=True,
    ring_depth=2,
    edge_space="stage",
):
    """2D convolution on [B,H,W,Cin_pad] with [Cout_pad,KH*KW*Cin_pad] weights."""
    batch, height, width, cin_pad = x.shape
    cout_pad, k_pad = weight.shape
    kh = kw = round((k_pad // cin_pad) ** 0.5)
    while kh * kw * cin_pad > k_pad or (kh + 1) * (kw + 1) * cin_pad <= k_pad:
        kh -= 1
        kw -= 1
    hp, wp = height + 2 * padding, width + 2 * padding
    oh, ow = (hp - kh) // stride + 1, (wp - kw) // stride + 1
    rows = batch * oh * ow
    rows_pad = -(-rows // 4) * 4
    if not fused:
        patches = call(
            C.unfold_kernel(batch, height, width, cin_pad, kh, kw, stride, padding),
            x.reshape(batch * height * width, cin_pad),
        )[0]
        out = linear(
            patches,
            weight,
            bias,
            activation=activation,
            residual=(
                None if residual is None else residual.reshape(rows_pad, cout_pad)
            ),
            fused=False,
        )
        return out.reshape(batch, oh, ow, cout_pad)
    k = C.kernel(
        batch,
        height,
        width,
        cin_pad,
        cout_pad,
        kh,
        kw,
        D.device().machine,
        stride=stride,
        padding=padding,
        activation=activation,
        residual=residual is not None,
        ring_depth=ring_depth,
        edge_space=edge_space,
    )
    args = [x.reshape(batch * height * width, cin_pad), weight, bias]
    if residual is not None:
        args.append(residual.reshape(rows_pad, cout_pad))
    return call(k, *args)[0].reshape(batch, oh, ow, cout_pad)


def attention(q, k, vt, *, fused=True):
    """Q/K are [heads, tokens, dim]; V is [heads, dim, tokens]."""
    if not fused:
        return unfused_attention(q, k, vt, precise_scores=False)
    heads, queries, dim = q.shape
    keys = k.shape[1]
    kernel = A.kernel(heads, queries, keys, dim, D.device().machine.count("VC"))
    tail = [tensor((np.arange(16) < keys % 16).astype(np.float16))] if keys % 16 else []
    return call(
        kernel,
        q.reshape(heads * queries, dim),
        k.reshape(heads * keys, dim),
        vt.reshape(heads * dim, keys),
        *tail,
    )[0].reshape(heads, queries, dim)


def softmax(x, *, fused=True):
    if not fused:
        return x.softmax().realize()
    rows, width = x.size // x.shape[-1], x.shape[-1]
    k = R.kernel("softmax", rows, width, D.device().machine.count("VC"))
    return call(k, x.reshape(rows, width))[0].reshape(*x.shape)


def layernorm(
    x,
    scale,
    shift,
    *,
    eps=1e-5,
    fused=True,
    backend="class",
    balance_norm=True,
):
    if not fused:
        return x.layernorm(scale, shift, eps).realize()
    rows, width = x.size // x.shape[-1], x.shape[-1]
    k = (
        G.GraphKernel(
            G.rows_graph(
                "layernorm",
                rows,
                width,
                D.device().machine,
                eps,
                balance_norm=balance_norm,
            )
        )
        if backend == "generated"
        else R.kernel("layernorm", rows, width, D.device().machine.count("VC"), eps)
    )
    return call(k, x.reshape(rows, width), scale, shift)[0].reshape(*x.shape)


def norm_linear(
    x,
    scale,
    shift,
    weight,
    bias,
    *,
    eps=1e-5,
    activation="none",
    fused=True,
    backend="class",
    ring_depth=2,
    balance_norm=True,
    mover_overlap=True,
    quant_backend="mover_quant",
):
    if not fused:
        return linear(
            layernorm(x, scale, shift, eps=eps, fused=False),
            weight,
            bias,
            activation=activation,
            fused=False,
        )
    rows, width = x.size // x.shape[-1], x.shape[-1]
    machine = D.device().machine
    if backend == "generated":
        k = G.norm_linear(
            rows,
            width,
            weight.shape[0],
            machine,
            eps,
            activation,
            ring_depth,
            balance_norm=balance_norm,
            mover_overlap=mover_overlap,
            quant_backend=quant_backend,
        )
        return call(k, x.reshape(rows, width), scale, shift, weight, bias)[0].reshape(
            *x.shape[:-1], weight.shape[0]
        )
    k = R.linear_kernel(
        rows,
        width,
        weight.shape[0],
        machine.count("MG"),
        machine.count("VC"),
        eps,
        activation,
        machine_json=rs.machine_json(machine),
    )
    return call(k, x.reshape(rows, width), scale, shift, weight, bias)[0].reshape(
        *x.shape[:-1], weight.shape[0]
    )


def linear(
    x,
    weight,
    bias,
    *,
    activation="none",
    residual=None,
    fused=True,
    class_capacity=True,
    backend="class",
    ring_depth=2,
    mover_overlap=True,
    quant_backend="mover_quant",
):
    """Flatten leading activation dimensions; weights have shape [N, K]."""
    if not fused:
        result = (x @ weight.T + bias).realize()
        if activation != "none":
            result = getattr(result, activation)().realize()
        return (result + residual).realize() if residual is not None else result
    rows, width = x.size // x.shape[-1], x.shape[-1]
    machine = D.device().machine
    if backend == "generated":
        k = G.linear(
            rows,
            width,
            weight.shape[0],
            machine,
            activation,
            residual is not None,
            ring_depth,
            mover_overlap=mover_overlap,
            quant_backend=quant_backend,
        )
        args = [x.reshape(rows, width), weight, bias]
        if residual is not None:
            args.append(residual.reshape(rows, weight.shape[0]))
        return call(k, *args)[0].reshape(*x.shape[:-1], weight.shape[0])
    k = L.kernel(
        rows,
        width,
        weight.shape[0],
        machine.count("MG"),
        machine.count("VC"),
        activation,
        residual is not None,
        class_capacity=class_capacity,
        machine_json=rs.machine_json(machine),
    )
    args = [x.reshape(rows, width), weight, bias]
    if residual is not None:
        args.append(residual.reshape(rows, weight.shape[0]))
    return call(k, *args)[0].reshape(*x.shape[:-1], weight.shape[0])


def mlp(
    x,
    w1,
    b1,
    w2,
    b2,
    *,
    activation="gelu",
    residual=None,
    fused=True,
    class_capacity=True,
    backend="class",
    ring_depth=2,
    edge_space="stage",
    compact_mlp=True,
    mover_overlap=True,
    quant_backend="mover_quant",
):
    """Linear, activation, linear, and optional residual in a single kernel."""
    if not fused:
        hidden = linear(x, w1, b1, activation=activation, fused=False)
        return linear(hidden, w2, b2, residual=residual, fused=False)
    rows, width = x.size // x.shape[-1], x.shape[-1]
    machine = D.device().machine
    if backend == "generated":
        k = G.mlp(
            rows,
            width,
            w1.shape[0],
            w2.shape[0],
            machine,
            activation,
            residual is not None,
            ring_depth,
            edge_space=edge_space,
            compact_mlp=compact_mlp,
            mover_overlap=mover_overlap,
            quant_backend=quant_backend,
        )
        args = [x.reshape(rows, width), w1, b1, w2, b2]
        if residual is not None:
            args.append(residual.reshape(rows, w2.shape[0]))
        return call(k, *args)[0].reshape(*x.shape[:-1], w2.shape[0])
    k = L.mlp_kernel(
        rows,
        width,
        w1.shape[0],
        w2.shape[0],
        machine.count("MG"),
        machine.count("VC"),
        activation,
        residual is not None,
        class_capacity=class_capacity,
        machine_json=rs.machine_json(machine),
    )
    args = [x.reshape(rows, width), w1, b1, w2, b2]
    if residual is not None:
        args.append(residual.reshape(rows, w2.shape[0]))
    return call(k, *args)[0].reshape(*x.shape[:-1], w2.shape[0])
