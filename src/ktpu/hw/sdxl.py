"""The SDXL UNet as a memory plan: every weight placed, every buffer's lifetime.

    shapes = {name: tuple(shape), ...}          # from the safetensors header
    plan = unet_plan(shapes, latent=(128, 128), arena_words=board.mem_words)
    place = plan.solve()

TOPOLOGY IS DERIVED, NOT ASSUMED: `modules` reads the block structure off the
parameter shapes, so a checkpoint with different widths or depths plans
correctly rather than being planned as the one this was written against.

Two results fall out. The weights fit only pre-quantised, and peak live memory
is set by the skip stack -- nine tensors held from the down path to the up path
-- rather than by the layer being computed.
"""

import re

from ktpu.hw import tensor as T
from ktpu.hw.memplan import AENTRY, BENTRY, CTILE, FLAT, Plan

#: SDXL fixes the attention head width at 64, so the head count is C // 64.
HEAD_DIM = 64

#: Query rows and key columns one flash-attention step holds. The score block is
#: what keeps attention memory linear in the sequence instead of quadratic.
Q_BLOCK, K_BLOCK = 128, 128


def pad_lanes(v):
    return -(-v // T.LANES) * T.LANES


def pad_k(v):
    return -(-v // T.KBLOCK) * T.KBLOCK


# ---- reading the architecture off the checkpoint ---------------------------
def modules(shapes: dict) -> list[dict]:
    """The UNet's modules in execution order, with shapes and resolutions.

    `shapes` maps a parameter name -- with the `model.diffusion_model.` prefix
    already stripped -- to its shape. Returns one dict per module with `name`,
    `kind` and the fields that kind needs, plus `scale`: the resolution divisor
    at which the module runs, 1 at the latent's own size.
    """
    groups = {}
    for k in shapes:
        m = re.match(r"^(input_blocks|output_blocks)\.(\d+)\.(\d+)\.", k)
        if m:
            key = (m.group(1), int(m.group(2)), int(m.group(3)))
        elif k.startswith("middle_block."):
            key = ("middle_block", int(k.split(".")[1]), 0)
        else:
            key = (k.split(".")[0], -1, 0)
        groups.setdefault(key, []).append(k)

    rank = {"time_embed": 0, "label_emb": 1, "input_blocks": 2,
            "middle_block": 3, "output_blocks": 4, "out": 5}
    out, scale = [], 1
    for key in sorted(groups, key=lambda t: (rank.get(t[0], 9), t[1], t[2])):
        stem = (f"{key[0]}.{key[1]}.{key[2]}" if key[1] >= 0 and
                key[0] != "middle_block" else
                f"{key[0]}.{key[1]}" if key[1] >= 0 else key[0])
        mod = _classify(stem, groups[key], shapes)
        if mod is None:
            continue
        if mod["kind"] == "conv" and mod.get("stride") == 2:
            out.append({**mod, "scale": scale})
            scale *= 2
            continue
        if mod["kind"] == "conv" and mod.get("up"):
            scale //= 2
        out.append({**mod, "scale": scale})
    return out


def _classify(stem, keys, shapes):
    """One module, named by the parameter shapes it holds. None for embeddings."""
    if stem in ("time_embed", "label_emb"):
        return {"name": stem, "kind": "mlp",
                "linears": sorted(k for k in keys if k.endswith(".weight"))}
    if any("transformer_blocks" in k for k in keys):
        depth = 1 + max(int(re.search(r"transformer_blocks\.(\d+)", k).group(1))
                        for k in keys if "transformer_blocks" in k)
        ch = shapes[f"{stem}.norm.weight"][0]
        ctx = max(shapes[k][1] for k in keys if k.endswith("attn2.to_k.weight"))
        return {"name": stem, "kind": "transformer", "c": ch, "depth": depth,
                "ctx": ctx}
    if any(k.endswith("in_layers.2.weight") for k in keys):
        cin = shapes[f"{stem}.in_layers.2.weight"][1]
        cout = shapes[f"{stem}.out_layers.3.weight"][0]
        return {"name": stem, "kind": "res", "cin": cin, "cout": cout,
                "skip": f"{stem}.skip_connection.weight" in shapes}
    convs = [k for k in keys if len(shapes[k]) == 4]
    if convs:
        s = shapes[convs[0]]
        # A lone conv inside an input_block past the stem is the stride-2
        # downsample; a conv sitting last in an output_block is the upsample.
        p = stem.split(".")
        down = p[0] == "input_blocks" and len(p) == 3 and p[1] != "0"
        up = p[0] == "output_blocks" and len(p) == 3 and p[2] != "0"
        return {"name": stem, "kind": "conv", "cin": s[1], "cout": s[0],
                "kh": s[2], "kw": s[3], "stride": 2 if down else 1, "up": up}
    if stem == "out":
        return {"name": stem, "kind": "conv", "cin": 320, "cout": 4,
                "kh": 3, "kw": 3, "stride": 1, "up": False}
    return None


# ---- the plan --------------------------------------------------------------
def unet_plan(shapes, latent=(128, 128), arena_words=1 << 27, preq=True,
              ctx_len=77, reuse=True):
    """A memory plan for one full forward pass.

    `latent` is the latent grid, 128x128 for a 1024px image. `preq` says the
    operand images are MXFP7 in memory, which is the only way the weights fit.
    `ctx_len` is the text-conditioning sequence length.

    Raises :class:`PlanError` from `solve` if the arena cannot hold it, with the
    weight and peak-activation figures in the message.
    """
    p = Plan(arena_words)
    mods = modules(shapes)
    h0, w0 = latent
    ctx = ctx_len

    p.input("latent", _flat(4, h0 * w0))
    p.weight("temb", FLAT(1, 1280), "the time/label embedding, already summed")
    p.input("context", _flat(ctx, 2048))

    cur, cin, skips = "latent", 4, []
    for i, mod in enumerate(mods):
        s = mod.get("scale", 1)
        hw = (h0 // s) * (w0 // s)
        if _first_of_up_block(mods, i):
            cur, cin = _cat(p, mod, cur, cin, skips.pop(), hw)
        kind = mod["kind"]
        if kind == "mlp":
            _mlp(p, shapes, mod)
        elif kind == "conv":
            hw_out = hw // 4 if mod.get("stride") == 2 else hw
            cur, cin = _conv(p, shapes, mod, cur, hw, hw_out, preq), mod["cout"]
        elif kind == "res":
            cur, cin = _res(p, shapes, mod, cur, hw, preq), mod["cout"]
        elif kind == "transformer":
            cur, cin = _transformer(p, shapes, mod, cur, hw, ctx, preq), mod["c"]
        if _last_of_down_block(mods, i):
            skips.append((cur, cin))
    p.tensors[cur].kind = "output"
    p.meta = {"modules": mods, "skips": len(skips), "preq": preq,
              "latent": latent, "ctx": ctx}
    return p


def _block_of(mod):
    p = mod["name"].split(".")
    return (p[0], p[1]) if len(p) >= 2 and p[1].isdigit() else (p[0], "")


def _last_of_down_block(mods, i):
    """True at the final module of an input_block: that output is a skip.

    THE SKIP STACK IS WHAT SETS PEAK MEMORY. Nine tensors are produced on the
    way down and consumed in reverse on the way up, so the first one is live
    across the entire network -- a planner without lifetimes cannot see this and
    a planner with them places everything else underneath it.
    """
    b = _block_of(mods[i])
    return b[0] == "input_blocks" and (
        i + 1 == len(mods) or _block_of(mods[i + 1]) != b)


def _first_of_up_block(mods, i):
    b = _block_of(mods[i])
    return b[0] == "output_blocks" and (i == 0 or _block_of(mods[i - 1]) != b)


def _cat(p, mod, cur, cin, skip, hw):
    """Concatenate the skip onto the up path. Channels add; space does not."""
    name, (sk, sc) = mod["name"], skip
    out = f"{name}.cat"
    p.act(out, FLAT(cin + sc, hw), f"{cur} ++ {sk}")
    p.step(f"{name}.cat", "vmap", reads=(cur, sk), writes=(out,))
    return out, cin + sc


def _flat(rows, cols):
    return FLAT(rows, cols)


def _mlp(p, shapes, mod):
    """The time and label embedding MLPs: two Linears, run once per forward."""
    for k in mod["linears"]:
        out, inp = shapes[k]
        p.weight(k, BENTRY(pad_lanes(out), pad_k(inp)))


def _conv(p, shapes, mod, src, hw_in, hw_out, preq):
    """A conv as kh*kw accumulating GEMMs over shifted views of the source."""
    name, cin, cout = mod["name"], mod["cin"], mod["cout"]
    taps = mod["kh"] * mod["kw"]
    wkey = f"{name}.weight"
    m, k, n = pad_lanes(hw_out), pad_k(cin), pad_lanes(cout)
    for t in range(taps):
        p.weight(f"{wkey}#{t}", BENTRY(n, k, preq), f"{name} tap {t}")
    if f"{name}.bias" in shapes:
        p.weight(f"{name}.bias", FLAT(1, cout))

    p.act(f"{name}.A", AENTRY(m, k), "the shifted views, packed by the host")
    p.step(f"{name}.pack", "repack", reads=(src,), writes=(f"{name}.A",),
           host_out=p.tensors[src].nbytes,
           host_in=taps * AENTRY(m, k).nbytes,
           taps=taps)
    p.act(f"{name}.C", CTILE(m, n))
    p.step(f"{name}.gemm", "matmul", reads=(f"{name}.A", f"{wkey}#0"),
           writes=(f"{name}.C",),
           wants={f"{name}.A": "aentry", f"{wkey}#0": "bentry"},
           gemms=taps, adds=taps - 1)
    out = f"{name}.out"
    p.act(out, FLAT(cout, hw_out))
    p.step(f"{name}.repack", "repack", reads=(f"{name}.C",), writes=(out,),
           wants={f"{name}.C": "ctile"},
           host_out=p.tensors[f"{name}.C"].nbytes,
           host_in=p.tensors[out].nbytes)
    return out


def _res(p, shapes, mod, src, hw, preq):
    """GroupNorm, SiLU, conv, +embedding, GroupNorm, SiLU, conv, + skip."""
    name, cin, cout = mod["name"], mod["cin"], mod["cout"]
    for i, (gk, ck, c) in enumerate(((f"{name}.in_layers.0", f"{name}.in_layers.2",
                                      cin),
                                     (f"{name}.out_layers.0",
                                      f"{name}.out_layers.3", cout))):
        p.act(f"{gk}.img", FLAT(c, hw), "gamma and beta, broadcast per element")
        p.step(f"{gk}.bcast", "repack", reads=(), writes=(f"{gk}.img",),
               host_in=2 * FLAT(c, hw).nbytes)
        p.act(f"{gk}.out", FLAT(c, hw))
        p.step(f"{gk}", "gnorm", reads=(src, f"{gk}.img"),
               writes=(f"{gk}.out",))
        p.act(f"{gk}.silu", FLAT(c, hw))
        p.step(f"{gk}.act", "vmap", reads=(f"{gk}.out",), writes=(f"{gk}.silu",))
        src = _conv(p, shapes, {"name": ck, "cin": c, "cout": cout, "kh": 3,
                                "kw": 3, "stride": 1}, f"{gk}.silu", hw, hw,
                    preq)
        if i == 0:
            p.weight(f"{name}.emb_layers.1.weight",
                     BENTRY(pad_lanes(cout), pad_k(1280), preq))
            p.act(f"{name}.emb", FLAT(cout, hw))
            p.step(f"{name}.embproj", "matmul", reads=("temb",),
                   writes=(f"{name}.emb",), host_in=FLAT(cout, hw).nbytes)
            nxt = f"{name}.h"
            p.act(nxt, FLAT(cout, hw))
            p.step(f"{name}.addemb", "vmap", reads=(src, f"{name}.emb"),
                   writes=(nxt,))
            src = nxt
    if mod.get("skip"):
        p.weight(f"{name}.skip_connection.weight",
                 BENTRY(pad_lanes(cout), pad_k(cin), preq))
    out = f"{name}.res"
    p.act(out, FLAT(cout, hw))
    p.step(f"{name}.residual", "vmap", reads=(src,), writes=(out,))
    return out


def _transformer(p, shapes, mod, src, hw, ctx, preq):
    """A SpatialTransformer: proj_in, `depth` BasicTransformerBlocks, proj_out."""
    name, c, depth = mod["name"], mod["c"], mod["depth"]
    heads = max(1, c // HEAD_DIM)
    for k in (f"{name}.proj_in.weight", f"{name}.proj_out.weight"):
        if k in shapes:
            s = shapes[k]
            p.weight(k, BENTRY(pad_lanes(s[0]), pad_k(s[1]), preq))
    cur = src
    for d in range(depth):
        cur = _btb(p, shapes, f"{name}.transformer_blocks.{d}", cur, c, hw,
                   ctx, heads, mod["ctx"], preq)
    out = f"{name}.out"
    p.act(out, FLAT(c, hw))
    p.step(f"{name}.proj_out", "matmul", reads=(cur,), writes=(out,))
    return out


def _btb(p, shapes, name, src, c, hw, ctx, heads, ctx_dim, preq):
    """Self-attention, cross-attention and a GEGLU feed-forward, all pre-normed.

    The score buffer is ONE flash block per head, not the full `hw x hw` matrix:
    at the 64x64 stage that is the difference between 32 KiB and 32 MiB a head.
    """
    for i, (norm, attn) in enumerate(((f"{name}.norm1", f"{name}.attn1"),
                                      (f"{name}.norm2", f"{name}.attn2"))):
        kv = ctx if i else hw
        kdim = ctx_dim if i else c
        p.act(f"{norm}.out", FLAT(c, hw))
        p.step(norm, "gnorm", reads=(src,), writes=(f"{norm}.out",),
               host_in=2 * FLAT(1, c).nbytes)
        for proj, kk in (("to_q", c), ("to_k", kdim), ("to_v", kdim)):
            key = f"{attn}.{proj}.weight"
            if key in shapes:
                s = shapes[key]
                p.weight(key, BENTRY(pad_lanes(s[0]), pad_k(s[1]), preq))
        p.act(f"{attn}.qkv", FLAT(3 * c, max(hw, kv)))
        p.step(f"{attn}.proj", "matmul", reads=(f"{norm}.out",),
               writes=(f"{attn}.qkv",), gemms=3)
        blocks = _blocks(hw) * _blocks(kv)
        p.act(f"{attn}.scores", FLAT(heads * Q_BLOCK, K_BLOCK),
              "one flash block per head, reused for every key block")
        p.act(f"{attn}.acc", FLAT(c, hw))
        p.step(f"{attn}.scores", "matmul", reads=(f"{attn}.qkv",),
               writes=(f"{attn}.scores",), gemms=heads * blocks, heads=heads)
        p.step(f"{attn}.softmax", "vmap",
               reads=(f"{attn}.scores", f"{attn}.qkv"), writes=(f"{attn}.acc",),
               runs=heads * blocks)
        key = f"{attn}.to_out.0.weight"
        if key in shapes:
            s = shapes[key]
            p.weight(key, BENTRY(pad_lanes(s[0]), pad_k(s[1]), preq))
        nxt = f"{attn}.out"
        p.act(nxt, FLAT(c, hw))
        p.step(f"{attn}.outproj", "matmul", reads=(f"{attn}.acc", src),
               writes=(nxt,))
        src = nxt

    p.act(f"{name}.norm3.out", FLAT(c, hw))
    p.step(f"{name}.norm3", "gnorm", reads=(src,), writes=(f"{name}.norm3.out",),
           host_in=2 * FLAT(1, c).nbytes)
    for key in (f"{name}.ff.net.0.proj.weight", f"{name}.ff.net.2.weight"):
        if key in shapes:
            s = shapes[key]
            p.weight(key, BENTRY(pad_lanes(s[0]), pad_k(s[1]), preq))
    p.act(f"{name}.ff.hidden", FLAT(8 * c, hw))
    p.step(f"{name}.ff.proj", "matmul", reads=(f"{name}.norm3.out",),
           writes=(f"{name}.ff.hidden",))
    p.act(f"{name}.ff.gate", FLAT(4 * c, hw))
    p.step(f"{name}.ff.geglu", "vmap", reads=(f"{name}.ff.hidden",),
           writes=(f"{name}.ff.gate",))
    out = f"{name}.out"
    p.act(out, FLAT(c, hw))
    p.step(f"{name}.ff.down", "matmul", reads=(f"{name}.ff.gate", src),
           writes=(out,))
    return out


def _blocks(n):
    return max(1, -(-n // K_BLOCK))


# ---- reporting -------------------------------------------------------------
#: `docs/driver/hardware-api.md` s2: PCIe is ~1.8 GB/s and is mapped
#: byte-identically to JTAG, so a weight address means the same thing on both.
XDMA_BPS = 1_800_000_000


def summary(place) -> str:
    """Weights, peak activations, traffic and where the time goes."""
    p = place.plan
    t = place.traffic()
    bulk = place.traffic(weight_bps=XDMA_BPS)
    gemms = sum(s.meta.get("gemms", 1 if s.kind == "matmul" else 0)
                for s in p.steps)
    kinds = {}
    for s in p.steps:
        kinds[s.kind] = kinds.get(s.kind, 0) + 1
    gb, mb = 2**30, 2**20
    wb = place.weight_words * 32
    return "\n".join([
        f"modules            {len(p.meta['modules'])}",
        f"tensors            {len(p.tensors):,}",
        f"kernels            {len(p.steps):,}  " +
        "  ".join(f"{k}={v:,}" for k, v in sorted(kinds.items())),
        f"cluster GEMMs      {gemms:,}",
        (f"weights resident   {wb / gb:.4f} GiB  "
         f"({'MXFP7' if p.meta['preq'] else 'fp16'} operand images)"),
        f"peak live acts     {place.peak_act_words * 32 / mb:,.1f} MiB",
        (f"arena used         {place.total_words * 32 / gb:.4f} GiB of "
         f"{p.arena_words * 32 / gb:.1f} GiB"),
        (f"host up / down     {t['up_bytes'] / gb:.3f} / "
         f"{t['down_bytes'] / gb:.3f} GiB"),
        (f"weight load        {t['weight_seconds'] / 3600:.2f} h over JTAG, "
         f"{bulk['weight_seconds']:.1f} s if the bulk write goes over PCIe"),
        (f"one forward        {t['step_seconds'] / 3600:.2f} h over JTAG, "
         f"weights already resident"),
    ])
