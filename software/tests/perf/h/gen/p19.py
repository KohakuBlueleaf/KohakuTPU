"""P19: matched loaded-body gamma. Each sysnode runs the same matmul shard body:
S0.1's linear shards (QKV, projection, FC1, FC2 at the P=4 quarter and P=2 half
widths) as 200-row y = x w^T, column tiles of 50x48 sub-tiles (200 x 192), the
tiles dealt round-robin over the sysnode's mgs, each mg sweeping all of K and
draining its tile. The runner's serial -> overlap -> serial triple (`Spec.triple`)
runs one package set on one binding set: each sysnode of a group alone (s_i), the
group together (s'_i), each alone again; groups are the adjacent pair, the end
pair and all four for quarter shards, the two pairs for half shards.

Modes: `disjoint` (each sysnode its own x and w: fixed per-shard traffic) and
`shared` (one x every sysnode reads, w its own: the read-only shared input).
Interferers, one at a time beside every body: `mover` (a posted 512 KiB copy) and
`pack` (vc[1] unpacking, doubling and packing 256 KiB). R = 8 repeats the body in
a sysnode loop on the quarter disjoint shards. The `aligned` fns start the same
bodies behind an on-card barrier (with a token ledger); their alone controls are
the triple fns' phases (same packages per body, other bindings).

Operands are exact witnesses (int7 under unit scales, every sum an integer below
2^11), so every y is compared word for word against x w^T; an output whose
sysnode did not run must stay POISON. gamma_critical = max(s'_i) / max(s_i) is
the analysis's, from the per-sysnode cycles of matched phases.

Hand L1, not the library path: S0 drives ktpugrad through one device on one
sysnode (`h.s0.run`, `dev.on_node`); hrun runs L1 modules and cannot launch the
library kernels per sysnode concurrently, so this is the matmul shard's hand-L1
equivalent (no bias, GELU or residual epilogue: the partial-output contract).
BLOCKED(library path per sysnode): the attention, LayerNorm and softmax shards.
BLOCKED(link interferer beside a lone sysnode): an interlink stream needs its far
sysnode's take, which a lone phase does not run; C3's paired samples supply it."""

import itertools
import json

import numpy as np

from h.gen import Module, Spec
from h.gen.body import HEAD, Body, host_tags
from h.gen.p6 import witness
from kohakutpu.application.runtime.card.common import MACHINES

M, G, GN, NK, CHUNK, ENTRY = 200, 50, 48, 2, 64, 128
SUB = G * GN
TILE_BYTES, COLS = SUB * 32, 4 * GN
#: name -> (K, N, P): S0.1's linear shards and the split each is a share of.
SHARDS = {
    "qkv_n576": (768, 576, 4),
    "qkv_n1152": (768, 1152, 2),
    "proj_n192": (768, 192, 4),
    "proj_n384": (768, 384, 2),
    "fc1_n768": (768, 768, 4),
    "fc1_n1536": (768, 1536, 2),
    "fc2_n192": (3072, 192, 4),
    "fc2_n384": (3072, 384, 2),
}
LOADED = ("proj_n192", "fc1_n768")
#: The interferers' bytes: a mover copy (one move), a vector stream of 2 KiB trips.
COPY_WORDS, PACK_TRIPS, PACK_WORDS = 16384, 128, 64
START = 1
REPEATS = 8

IMAGES = f"""
image dbl(n, x, y)
    desc a0 = x walk=(32, {PACK_WORDS})
    desc a1 = y walk=(32, {PACK_WORDS})
    ainc a0 += {PACK_WORDS * 32}
    ainc a1 += {PACK_WORDS * 32}
    seti s0 = n
    loop s0
        vfill a0 -> l1[0] rel
        vsync unpack on=fill
        for j in 0..8
            vunpk v{{j}} <- l1[j*8] f16
        for j in 0..8
            vadd v{{8 + j}}, v{{j}}, v{{j}}
        for j in 0..8
            vpack v{{8 + j}} -> l1[{PACK_WORDS} + j*8] f16
        vsync drain on=pack
        vdrain a1 <- l1[{PACK_WORDS}] rel
        vsync fill on=drain
        vsync pack on=drain
    vsync fe on=drain
    halt
"""


def mgs(board: str) -> int:
    machine = json.loads((MACHINES / f"{board}.json").read_text("utf-8"))["machine"]
    return machine["mg"]["count"]


def b_at(t: int, c: int, chunks: int) -> int:
    """Column tile t's K chunk c in w (B packing: tiles, then chunks)."""
    return (t * chunks + c) * GN * NK * ENTRY


def body(b: Body, s: int, shard: str, x: str, units: int) -> None:
    """Sysnode s's shard: tiles dealt round-robin over `units` mgs, a round at a time."""
    k, n, _ = SHARDS[shard]
    chunks = k // CHUNK
    tiles = n // COLS
    for first in range(0, tiles, units):
        used = range(first, min(first + units, tiles))
        for t in used:
            lines = [f"send mg[{t - first}]"]
            for c in range(chunks):
                lines += [
                    f"    fill A{c % 2} <- {x} + {c * G * NK * ENTRY} n={G * NK}",
                    f"    fill B{c % 2} <- w{s} + {b_at(t, c, chunks)} n={GN * NK}",
                    f"    gemm {G}x{GN}x{NK} a=A{c % 2} b=B{c % 2} acc={int(c > 0)}",
                ]
            lines.append(f"    drain y{s} + {t * TILE_BYTES} n={SUB}")
            b.add(s, *lines)
        b.add(s, *(f"wait mg[{t - first}]" for t in used))


def program(sysnodes, shard: str, mode: str, load: str, repeats: int, units: int, b):
    for s in sysnodes:
        x = "x" if mode == "shared" else f"x{s}"
        if load == "mover":
            walk = f"({COPY_WORDS}, 32)"
            b.add(s, "t = move posted", f"    copy w(u{s}, {walk}) -> w(v{s}, {walk})")
        if load == "pack":
            b.add(s, "send vc[1]", f"    run dbl({PACK_TRIPS}, u{s}, v{s})")
        if repeats > 1:
            b.add(s, "r3 = set 0", f"r4 = set {repeats}", "label rep")
        body(b, s, shard, x, units)
        if repeats > 1:
            b.add(s, "r3 = add r3, 1", "goto rep if r3 < r4")
        if load == "mover":
            b.add(s, "wait moves t")
        if load == "pack":
            b.add(s, "wait vc[1]")


def params(sysnodes, shard: str, mode: str, load: str) -> str:
    k, n, _ = SHARDS[shard]
    out = [f"x: mx7a[{M}, {k}] @dram(gm={G}, nk={NK})"] if mode == "shared" else []
    for s in sysnodes:
        if mode != "shared":
            out.append(f"x{s}: mx7a[{M}, {k}] @dram(gm={G}, nk={NK})")
        out += [
            f"w{s}: mx7b[{n}, {k}] @dram(gn={GN}, nk={NK})",
            f"y{s}: f16[{M}, {n}] @dram(tile={G}x{GN})",
        ]
        if load:
            words = COPY_WORDS if load == "mover" else PACK_TRIPS * PACK_WORDS
            out += [f"u{s}: f16[{16 * words}] @dram", f"v{s}: f16[{16 * words}] @dram"]
    return ", ".join(out)


def operands(sysnodes, shard: str, mode: str, load: str) -> dict:
    """Witness x and w (x shared or one each), and the interferer's tag words."""
    k, n, _ = SHARDS[shard]
    out = {}
    for s in sysnodes:
        rng = np.random.default_rng([k, n, s])
        x, out[f"w{s}"] = witness(rng, M, n, k)
        out["x" if mode == "shared" else f"x{s}"] = x
    if load:
        words = COPY_WORDS if load == "mover" else PACK_TRIPS * PACK_WORDS
        tags = host_tags({f"u{s}": (32 * words, np.float16, s) for s in sysnodes})
        out |= tags(0)
    return out


def reference(sysnodes, mode: str, load: str):
    def want(ins: dict) -> dict:
        out = {}
        for s in sysnodes:
            x = ins["x" if mode == "shared" else f"x{s}"].astype(np.float64)
            out[f"y{s}"] = x @ ins[f"w{s}"].astype(np.float64).T
            if load == "mover":
                out[f"v{s}"] = ins[f"u{s}"]
            if load == "pack":
                out[f"v{s}"] = ins[f"u{s}"] * np.float16(2)
        return out

    return want


def fn(sysnodes, shard, mode, load, repeats, units, aligned: tuple | None):
    """One fn; `aligned`: the group started behind a barrier (else a triple)."""
    group = aligned or sysnodes
    b = Body(ledger=aligned is not None)
    if aligned:
        b.barrier(list(aligned), START)
    program(group, shard, mode, load, repeats, units, b)
    tag = f"_aligned{''.join(map(str, aligned))}" if aligned else ""
    name = (
        f"p19_{shard}_{mode}"
        + (f"_{load}" if load else "")
        + (f"_r{repeats}" if repeats > 1 else "")
    )
    name += tag
    outs = [f"y{s}" for s in group] + ([f"v{s}" for s in group] if load else [])
    _, _, p = SHARDS[shard]
    pairs = [(0, 1), (0, 3)]
    groups = [
        g for g in pairs + ([(0, 1, 2, 3)] if p == 4 else []) if set(g) <= set(sysnodes)
    ]
    given = operands(group, shard, mode, load)
    spec = Spec(
        tuple(outs) + (("slots",) if aligned else ()),
        reference(group, mode, load),
        inputs=lambda _: given,
        fresh=False,
        ledger=b.ledger_of() if aligned else None,
        triple=aligned is None,
        overlaps=() if aligned else tuple(groups),
        owners={} if aligned else {o: int(o[1:]) for o in outs},
    )
    return name, b.fn(name, params(group, shard, mode, load)), spec


def generate(sysnodes: tuple[int, ...], board: str) -> Module:
    units = mgs(board)
    cases = [
        (sh, mode, "", 1, None)
        for sh, mode in itertools.product(SHARDS, ("disjoint", "shared"))
    ]
    cases += [
        (sh, "disjoint", "", REPEATS, None) for sh in SHARDS if SHARDS[sh][2] == 4
    ]
    cases += [
        (sh, "disjoint", load, 1, None) for sh in LOADED for load in ("mover", "pack")
    ]
    pairs = [
        g for g in ((0, 1), tuple(sysnodes)) if set(g) <= set(sysnodes) and len(g) > 1
    ]
    cases += [
        (sh, mode, "", 1, g)
        for sh, mode, g in itertools.product(
            [s for s in SHARDS if SHARDS[s][2] == 4],
            ("disjoint", "shared"),
            dict.fromkeys(pairs),
        )
    ]
    text, specs = HEAD + IMAGES, {}
    for shard, mode, load, repeats, aligned in cases:
        name, src, spec = fn(sysnodes, shard, mode, load, repeats, units, aligned)
        text += src
        specs[name] = spec
    return Module(text, specs)
