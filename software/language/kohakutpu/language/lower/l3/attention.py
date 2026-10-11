"""L3 -> L2 for attention: y = (2^(s - max s) v) / sum 2^(s - max s), s = q k^T.

`exact`: two passes over each 256-row query tile -- scores of every key tile
drained, then P and l on a vector core, then P v and the division. The
L3 body must have exactly this dataflow; names and sizes come from it.
"""

from kohakutpu.language.l3 import nodes as N
from kohakutpu.language.lower.l3.common import (
    CLUSTERS,
    CORES,
    PlanError,
    lets,
    returned,
    size,
)
from kohakutpu.language.text.writer import Text

TILE = 256
#: The op sequence the attention form implements.
SHAPE = (
    "quantise",
    "quantise",
    "mmt",
    "reduce.max",
    "sub",
    "exp2",
    "reduce.sum",
    "quantise",
    "transpose",
    "quantise",
    "mmt",
    "inv",
    "mul",
)


def check(fn) -> tuple:
    body = lets(fn)
    if tuple(s.op for s in body) != SHAPE:
        raise PlanError(f"{fn.name}: not the attention dataflow {SHAPE}")
    q, k, s, m, d, p, l, pq, vt, vq, o, r, y = body
    ok = (
        s.args[0].name == q.name
        and s.args[1].name == k.name
        and m.args[0].name == s.name
        and d.args[0].name == s.name
        and d.args[1].name == m.name
        and p.args[0].name == d.name
        and l.args[0].name == p.name
        and pq.args[0].name == p.name
        and vq.args[0].name == vt.name
        and o.args[0].name == pq.name
        and o.args[1].name == vq.name
        and r.args[0].name == l.name
        and y.args[0].name == o.name
        and y.args[1].name == r.name
        and returned(fn) == y.name
    )
    if not ok:
        raise PlanError(f"{fn.name}: the attention ops are not wired as attention")
    return q.args[0].name, k.args[0].name, vt.args[0].name, y.name


def heads(fn, dims: dict) -> str:
    """Per-head attention (`map h in tiles(R, n)` around the attention body,
    each head's y stored) in the online form: flash.ktpu's `flash.l2` shape
    for R / n heads of n = 1024 keys, d = 64."""
    maps = [s for s in fn.body if isinstance(s, N.Map)]
    tiles = {s.name: s.value for s in fn.body if isinstance(s, N.Tile)}
    if len(maps) != 1 or len(maps[0].vars) != 1:
        raise PlanError("per-head attention is one map over heads")
    ((_, dom),) = maps[0].vars
    n = tiles.get(dom.size, dom.size) if isinstance(dom, N.Tiles) else None
    inner = [s for s in maps[0].body if isinstance(s, N.Let)]
    shell = N.Fn(fn.name, fn.params, fn.ret, (*inner, N.Return(N.Ref(inner[-1].name))))
    qn, kn, vn, _ = check(shell)
    r, d = size(fn.params[0][1].shape, dims)
    if n != 1024 or d != 64 or r % n:
        raise PlanError(f"the online form takes heads of 1024 x 64, got {n} x {d}")
    g = 8 * (r // n)
    return FLASH.format(name=fn.name, q=qn, k=kn, v=vn, y=returned(fn), r=r, g=g)


#: The online form, per 128-query scores tile g (see flash.ktpu).
FLASH = """fn {name}.l2({q}: mx7a[{r}, 64] @dram, {k}: mx7b[{r}, 64] @dram, {v}: mx7b[64, {r}] @dram, {y}: f16[{r}, 64] @dram)
    s = buffer : f16[512, 1024] @dram
    pb = buffer : mx7a[128, 1024] @dram
    o = buffer : f16[512, 64] @dram
    lb = buffer : f16[512, 4] @dram
    ib = buffer : f16[16, 32] @dram
    ones = buffer : mx7b[4, 512] @dram
    par c in 0..1 on vc[0]
        store ones <- quantise 1.0
    par g in 0..{g} on mg[2*(g % 2)]
        for kh in 0..2
            acc = gemm {q}[g*128 : +128, :], {k}[(g/8)*1024 + kh*512 : +512, :] k=64 : f32[128, 512] @acc
            store s[(g%4)*128 : +128, kh*512 : +512] <- drain acc
        for r in 0..4
            par h in 0..2 on vc[h]
                st = load s[(g%4)*128 + r*32 : +32, h*512 : +512] : f16[32, 512] @l1
                m0 = reduce.max st[:, 0 : +32] : f32[32]
                m1 = add m0, 49156.0 : f32[32]
                i = sub m1, 49152.0 : f32[32]
                d = sub st, i[:, *] : f32[32, 512]
                p = exp2 d : f16[32, 512]
                store pb[r*32 : +32, h*512 : +512] <- quantise p
                store ib[((4*g + r)%8)*2 + h, :] <- i
            par kh in 0..2 on mg[1 + 2*kh]
                pv = gemm pb[r*32 : +32, kh*512 : +512], {v}[:, (g/8)*1024 + kh*512 : +512] k=512 : f32[32, 64] @acc
                store o[(((4*g + r)%8)*2 + kh)*32 : +32, :] <- drain pv
                pl = gemm pb[r*32 : +32, kh*512 : +512], ones k=512 : f32[32, 4] @acc
                store lb[(((4*g + r)%8)*2 + kh)*32 : +32, :] <- drain pl
            par h in 0..2 on vc[h]
                oa = load o[(((4*g + r)%8)*2)*32 + 16*h : +16, :] : f16[16, 64] @l1
                ob = load o[(((4*g + r)%8)*2 + 1)*32 + 16*h : +16, :] : f16[16, 64] @l1
                la = load lb[(((4*g + r)%8)*2)*32 + 16*h : +16, 0] : f16[16] @l1
                lc = load lb[(((4*g + r)%8)*2 + 1)*32 + 16*h : +16, 0] : f16[16] @l1
                ia = load ib[((4*g + r)%8)*2, 16*h : +16] : f16[16] @l1
                ic = load ib[((4*g + r)%8)*2 + 1, 16*h : +16] : f16[16] @l1
                mx = max ia, ic : f32[16]
                da = sub ia, mx : f32[16]
                ea = exp2 da : f32[16]
                dc = sub ic, mx : f32[16]
                ec = exp2 dc : f32[16]
                xa = mul la, ea : f32[16]
                den = fma lc, ec, xa : f32[16]
                rd = inv den : f32[16]
                wa = mul ea, rd : f32[16]
                wc = mul ec, rd : f32[16]
                ya = mul oa, wa[:, *] : f32[16, 64]
                store {y}[(4*g + r)*32 + 16*h : +16, :] <- fma ob, wc[:, *], ya
"""


def plan(fn, dims: dict, softmax: str) -> str:
    qn, kn, vn, yn = check(fn)
    params = {p: size(t.shape, dims) for p, t in fn.params}
    n, dd = params[qn]
    m, _ = params[kn]
    if n % TILE or m % TILE:
        raise PlanError(f"{n} queries x {m} keys are not whole {TILE} tiles")
    if softmax != "exact":
        raise PlanError(f"no attention form for softmax={softmax}")
    out = Text()
    out(
        f"fn {fn.name}.l2({qn}: mx7a[{n}, {dd}] @dram, {kn}: mx7b[{m}, {dd}] @dram, "
        f"{vn}: mx7b[{dd}, {m}] @dram, {yn}: f16[{n}, {dd}] @dram)"
    )
    with out.block():
        out(f"s = buffer : f16[{n}, {m}] @dram")
        out(f"p = buffer : mx7a[{n}, {m}] @dram")
        out(f"o = buffer : f16[{n}, {dd}] @dram")
        out(f"l = buffer : f32[{n}] @dram")
        out(f"par u in 0..{n // TILE} on mg[u % {CLUSTERS}]")
        with out.block():
            out(f"for t in 0..{m // TILE}")
            with out.block():
                out(
                    f"acc = gemm {qn}[u*{TILE} : +{TILE}, :], {kn}[t*{TILE} : +{TILE}, :] "
                    f"k={dd} : f32[{TILE}, {TILE}] @acc"
                )
                out(f"store s[u*{TILE} : +{TILE}, t*{TILE} : +{TILE}] <- drain acc")
            out(f"par c in 0..1 on vc[u % {CORES}]")
            with out.block():
                out(f"st = load s[u*{TILE} : +{TILE}, :] : f16[{TILE}, {m}] @l1")
                out(f"m = reduce.max st : f32[{TILE}]")
                out(f"d = sub st, m[:, *] : f32[{TILE}, {m}]")
                out(f"pt = exp2 d : f16[{TILE}, {m}]")
                out(f"store l[u*{TILE} : +{TILE}] <- reduce.sum pt")
                out(f"store p[u*{TILE} : +{TILE}, :] <- quantise pt")
            out(
                f"acc = gemm p[u*{TILE} : +{TILE}, :], {vn} k={m} : f32[{TILE}, {dd}] @acc"
            )
            out(f"store o[u*{TILE} : +{TILE}, :] <- drain acc")
            out(f"par c in 0..1 on vc[u % {CORES}]")
            with out.block():
                out(f"lt = load l[u*{TILE} : +{TILE}] : f32[{TILE}] @l1")
                out(f"ot = load o[u*{TILE} : +{TILE}, :] : f16[{TILE}, {dd}] @l1")
                out(f"r = inv lt : f32[{TILE}]")
                out(f"store {yn}[u*{TILE} : +{TILE}, :] <- mul ot, r[:, *]")
    return out.text()


__all__ = ["plan"]
