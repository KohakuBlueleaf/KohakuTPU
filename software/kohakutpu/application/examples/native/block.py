"""Eager encoder block using selectable native ops, no graph capture/whole-model compile."""

from kohakutpu.application.api import native


def block(
    x,
    weights,
    mask,
    *,
    fused=True,
    ring_depth=2,
    activation="gelu",
    query_blocked=False,
    mlp_space="stage",
    attn_space="dram",
    fast_attn=True,
    compact_mlp=True,
    balance_norm=True,
    mover_overlap=True,
    quant_backend="mover_quant",
):
    scale1, shift1, qweight, qbias, pweight, pbias, scale2, shift2, w1, b1, w2, b2 = (
        weights
    )
    width = x.shape[-1]
    options = {
        "fused": fused,
        "backend": "generated",
        "ring_depth": ring_depth,
        "mover_overlap": mover_overlap,
        "quant_backend": quant_backend,
    }
    qkv = native.norm_linear(
        x,
        scale1,
        shift1,
        qweight,
        qbias,
        eps=1e-6,
        balance_norm=balance_norm,
        **options,
    )
    context = native.projection_attention(
        qkv,
        mask,
        heads=width // 64,
        dim=64,
        fused=fused,
        query_blocked=query_blocked,
        space=attn_space,
        fast_attn=fast_attn,
        mover_overlap=mover_overlap,
        quant_backend=quant_backend,
    )
    projected = native.linear(context, pweight, pbias, **options)
    skip = (x + projected).realize()
    normalized = native.layernorm(
        skip,
        scale2,
        shift2,
        eps=1e-6,
        fused=fused,
        backend="generated",
        balance_norm=balance_norm,
    )
    hidden = native.mlp(
        normalized,
        w1,
        b1,
        w2,
        b2,
        activation=activation,
        edge_space=mlp_space,
        compact_mlp=compact_mlp,
        **options,
    )
    output = (skip + hidden).realize()
    return output, {
        "qkv": qkv,
        "context": context,
        "projected": projected,
        "normalized": normalized,
        "mlp": hidden,
        "output": output,
    }


def vit_forward(
    patches,
    pos_cls,
    patch_weights,
    blocks,
    head_weights,
    mask,
    *,
    fused=True,
    ring_depth=2,
    activation="gelu",
    query_blocked=True,
    mlp_space="stage",
    attn_space="stage",
    fast_attn=True,
    compact_mlp=True,
    balance_norm=True,
    mover_overlap=True,
    quant_backend="mover_quant",
):
    pweight, pbias = patch_weights
    nscale, nshift, hweight, hbias = head_weights
    options = {
        "fused": fused,
        "backend": "generated",
        "ring_depth": ring_depth,
        "mover_overlap": mover_overlap,
        "quant_backend": quant_backend,
    }
    x = (native.linear(patches, pweight, pbias, **options) + pos_cls).realize()
    states = {"embedding": x}
    for i, bweights in enumerate(blocks, 1):
        x, bstates = block(
            x,
            bweights,
            mask,
            fused=fused,
            ring_depth=ring_depth,
            activation=activation,
            query_blocked=query_blocked,
            mlp_space=mlp_space,
            attn_space=attn_space,
            fast_attn=fast_attn,
            compact_mlp=compact_mlp,
            balance_norm=balance_norm,
            mover_overlap=mover_overlap,
            quant_backend=quant_backend,
        )
        states[f"block_{i}"] = x
        if i == 1:
            states.update(bstates)
    logits = native.norm_linear(
        x,
        nscale,
        nshift,
        hweight,
        hbias,
        eps=1e-6,
        balance_norm=balance_norm,
        **options,
    )
    states["logits"] = logits
    return logits, states
