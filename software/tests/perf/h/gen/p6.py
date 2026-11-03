"""P6: K-split reduction across sysnodes, two separate algorithms.

FP22 peer tree: every participant's mg sums its K slice of y = x w^T (K = 768 cut
into equal runs of 64-wide chunks, slice i to the i-th sysnode of its tree in
order) at full accumulator precision. A receiver whose own sweeps are done rings
READY to one contributor at a time; the contributor drains its accumulator into
the receiver's (buffer 2, an FP22 add) and a COUNTed entry right behind it, which
the receiver takes (`fill A0 recv`) before it rings the next: one inbound source
per receiving mg, consumption established by the counted marker, not a bell. The
root drains y, rings DONE to its tree, and the first other sysnode copies y's first
and last two words to `seen` right after its wait.

Topologies: one contributor on every route from and into sysnode 0 (hops 1-3), two
and three serialized contributors, two receivers at once (0->1 beside 3->2), the
adjacent tree 0->1, 3->2, 1->2, and on the 200x768 output the rotating owners
(sysnode t owns column tile t; at step j = 1..3 sysnode i adds its mg[1] partial
into owner i - j's mg[0], READY slot per step). Late variants delay the READY ring
or the payload drain by LATE cycles. An unsplit control sums all of K on one mg.

Outputs: a 4x4 tile (one sub-tile), 200x192 (one 50x48 tile), 100x768 (two 25x96
tiles) and 200x768 (four 50x48 tiles), each tile at most 4096 sub-tiles of an
accumulator. Data: `witness` (int7 operands under unit scales, every partial and
sum an integer below 2^11, exact), `seeded`, `cancel` (K's second half the first
with w negated), `large` (sums past f16's 65504, partials below), `tiny` (sums near
2^-14, where a drain flushes). References: the FP22 sum in the program's fixed
order (`fp22`, `issue`, `drained` model the accumulator exactly as ktpu-sim's
NUM.ACC), compared numerically; `witness` word for word.

f16 partials: every participant drains its slice's sum as f16 into one slab of
`part` (shared DRAM), rings the root fenced; the root copies each slab's first
and last two words to `seenp` at acquisition, then its vc[0] sums the slabs in
participant order, adds bias then res (both full [M, N] tensors) in E8M15 and
packs f16 to z; DONE then publishes z, observed into `seenz`. Endpoint bytes for
[200, 768]: FP22 wire partial 614,400 B, the tree's three edges 1,843,200 B before
headers; f16 algorithm 2,764,800 B, 2,457,600 B past one output write.

Not here: the f16 algorithm on the 4x4 tile (one word, under the sum's 16-word
trip). BLOCKED(subphase stamps): no fixed image stamps ready, payload, add+marker
and publish apart; a run's cycles are their composite."""

import numpy as np

from h.gen import Module, Spec
from h.gen.body import HEAD, Body
from kohakutpu.language.numerics import e8m15, fp16, mxfp7

K, NK, CHUNK, ENTRY = 768, 2, 64, 128
CHUNKS = K // CHUNK
#: name -> (M, N, G, GN): an output and its gemm tile of G x GN 4x4 sub-tiles.
SHAPES = {
    "t4": (4, 4, 1, 1),
    "s200x192": (200, 192, 50, 48),
    "s100x768": (100, 768, 25, 96),
    "s200x768": (200, 768, 50, 48),
}
KINDS = ("witness", "seeded", "cancel", "large", "tiny")
#: Edges (contributor, receiver), each receiver's in arrival order, each
#: contributor's inbound edges before its outbound one.
TOPOLOGIES = (
    ((0, 1),),
    ((0, 2),),
    ((0, 3),),
    ((1, 0),),
    ((2, 0),),
    ((3, 0),),
    ((0, 1), (2, 1)),
    ((1, 0), (2, 0), (3, 0)),
    ((2, 3), (1, 3), (0, 3)),
    ((0, 1), (3, 2)),
    ((0, 1), (3, 2), (1, 2)),
)
PAIR, STAR, TREE = TOPOLOGIES[0], TOPOLOGIES[7], TOPOLOGIES[10]
LATE = 20_000
READY, DONE, PART, STEP = 1, 2, 3, 4
#: The vector sum's words a trip (two registers of 8 words an operand).
TRIP = 16
SBIAS = mxfp7.SBIAS


def witness(rng, m: int, n: int, k: int) -> tuple[np.ndarray, np.ndarray]:
    """x [m, k], w [n, k] in {-1, 0, 1} with 63 at one column of every K block
    (zero in the other operand): unit scales, so x w^T is exact."""
    x = rng.integers(-1, 2, (m, k)).astype(np.float16)
    w = rng.integers(-1, 2, (n, k)).astype(np.float16)
    for blk in range(k // mxfp7.KBLOCK):
        kx, kw = blk * 32 + blk % 16, blk * 32 + 16 + blk % 16
        x[:, kx], w[:, kx], w[:, kw], x[:, kw] = 63, 0, 63, 0
    return x, w


def data(kind: str, m: int, n: int) -> dict:
    """Host f16 x [m, K], w [n, K], bias and res [m, n] of a data kind."""
    rng = np.random.default_rng(list(map(ord, kind)) + [m, n])

    def normal(*shape, scale=1.0, at=0.0):
        return (at + scale * rng.standard_normal(shape)).astype(np.float16)

    if kind == "witness":
        x, w = witness(rng, m, n, K)
        res = [rng.integers(-3, 4, (m, n)).astype(np.float16) for _ in "br"]
        return dict(x=x, w=w, bias=res[0], res=res[1])
    if kind == "cancel":
        x, w = normal(m, K, scale=4.0), normal(n, K, scale=4.0)
        x[:, K // 2 :], w[:, K // 2 :] = x[:, : K // 2], -w[:, : K // 2]
    elif kind == "large":
        x, w = normal(m, K, scale=0.5, at=12.0), normal(n, K, scale=0.5, at=12.0)
    elif kind == "tiny":
        x, w = normal(m, K, scale=2.0**-9), normal(n, K, scale=2.0**-9)
    else:
        x, w = normal(m, K, scale=0.05), normal(n, K, scale=0.05)
    scale = 2.0**-12 if kind == "tiny" else 0.0 if kind == "large" else 0.1
    return dict(x=x, w=w, bias=normal(m, n, scale=scale), res=normal(m, n, scale=scale))


def operand(v: np.ndarray):
    """An f16 operand as K-pair entries quantise it: int7 (rows, blocks, 32), the
    biased scale exponents and m8 (rows, blocks)."""
    q, es, m8 = mxfp7.quantise_fp16(mxfp7.lift(v, NK))
    return q.reshape(len(v), -1, mxfp7.KBLOCK).astype(np.float64), es + SBIAS, m8


def fp22(v):
    """FP22 (S1 E7 M14) round to nearest even; below 2^-62 zero."""
    m, e = np.frexp(v)
    r = np.ldexp(np.rint(np.ldexp(m, 15)), e - 15)
    return np.where(np.abs(r) < 2.0**-62, 0.0, r)


def block(a, b, kb: int):
    """K block kb's integer partial times both m8, and its exponent sum."""
    (qa, ea, ma), (qb, eb, mb) = a, b
    part = (qa[:, kb] @ qb[:, kb].T).astype(np.int64)
    return part * (ma[:, kb, None] * mb[None, :, kb]), ea[:, kb, None] + eb[None, :, kb]


def issue(a, b, c: int):
    """Chunk c's sweep: its K pair aligned to the larger exponent sum, the other
    truncated, then FP22 (NUM.ACC.008)."""
    (p0, s0), (p1, s1) = block(a, b, NK * c), block(a, b, NK * c + 1)
    d = np.minimum(np.abs(s1 - s0), 30)
    part = np.where(s1 > s0, (p0 >> d) + p1, p0 + (p1 >> d))
    at = np.maximum(s0, s1) - mxfp7.ANCHOR - 6
    return fp22(np.ldexp(part.astype(np.float64), at.astype(np.int32)))


def partial(a, b, chunks) -> np.ndarray:
    """An mg's accumulator after its chunks' sweeps, in order."""
    acc = issue(a, b, chunks[0])
    for c in chunks[1:]:
        acc = fp22(acc + issue(a, b, c))
    return acc


def drained(v):
    """FP22 to f16 at a drain: RNE to 10 bits, under 2^-14 zero, past it 65504."""
    a = np.abs(v)
    m, e = np.frexp(a)
    f = np.rint((2 * m - 1) * 1024)
    e = np.where(f == 1024, e + 1, e)
    f = np.where(f == 1024, 0, f)
    big = np.copysign(
        np.where(e + 14 >= 31, fp16.MAX, np.ldexp(1 + f / 1024, e - 1)), v
    )
    return np.where((a == 0) | (e + 14 <= 0), 0.0, big)


def vector_sum(terms) -> np.ndarray:
    """The core's ordered E8M15 sum (vadd: a + c), packed f16."""
    acc = terms[0]
    for t in terms[1:]:
        acc = e8m15.fma(acc, 1.0, t)
    return fp16.write(acc).astype(np.float64)


def tiled(y: np.ndarray, g: int, gn: int) -> np.ndarray:
    """[M, N] in the drain's order: G x GN tiles row-major, sub-tiles row-major."""
    m, n = y.shape
    t = y.reshape(m // (4 * g), g, 4, n // (4 * gn), gn, 4).transpose(0, 3, 1, 4, 2, 5)
    return t.astype(np.float16).ravel()


def slices(group) -> dict:
    """Each sysnode's chunks: slice i of len(group) to the i-th of `group`."""
    p = len(group)
    return {
        s: list(range(i * CHUNKS // p, (i + 1) * CHUNKS // p))
        for i, s in enumerate(group)
    }


def sweeps(b: Body, s: int, unit: int, shape: str, t: int, chunks) -> None:
    """Sysnode s's mg[unit] summing column tile t over `chunks`, then waited."""
    _, _, g, gn = SHAPES[shape]
    lines = [f"send mg[{unit}]"]
    for i, c in enumerate(chunks):
        lines += [
            f"    fill A{c % 2} <- x + {c * g * NK * ENTRY} n={g * NK}",
            f"    fill B{c % 2} <- w + {(t * CHUNKS + c) * gn * NK * ENTRY} n={gn * NK}",
            f"    gemm {g}x{gn}x{NK} a=A{c % 2} b=B{c % 2} acc={int(i > 0)}",
        ]
    b.add(s, *lines, f"wait mg[{unit}]")


def add(b: Body, c: int, unit: int, d: int, sub: int, slot: int, late: bool) -> None:
    """c's mg[unit] adds into d's mg[0] once d is ready; d takes the marker."""
    b.ring(d, c, slot)
    b.take(c, slot, 1)
    if late:
        b.add(c, f"settle {LATE}")
    b.add(
        c,
        f"send mg[{unit}]",
        f"    drain n={sub} to=mesh{d}.mg[0] buf=2 l1=0",
        f"    drain n=4 to=mesh{d}.mg[0] buf=0 l1=0 count",
        f"wait mg[{unit}]",
    )
    b.add(d, "send mg[0]", "    fill A0 recv n=1", "wait mg[0]")


def operands(shape: str):
    m, n, g, gn = SHAPES[shape]
    return f"x: mx7a[{m}, {K}] @dram(gm={g}, nk={NK}), w: mx7b[{n}, {K}] @dram(gn={gn}, nk={NK})"


def ytype(shape: str, extra: str = "") -> str:
    m, n, g, gn = SHAPES[shape]
    return f"f16[{m}, {n}] @dram(tile={g}x{gn}{extra})"


def groups(edges) -> list[list[int]]:
    """The trees of `edges`: each its sysnodes, sorted."""
    out: list[set] = []
    for c, d in edges:
        hit = [g for g in out if c in g or d in g]
        merged = set().union({c, d}, *hit)
        out = [g for g in out if g not in hit] + [merged]
    return [sorted(g) for g in out]


def model(ins: dict, shape: str):
    """Quantised operands and, per column tile, its w operand."""
    _, n, _, gn = SHAPES[shape]
    a, bw = operand(ins["x"]), operand(ins["w"])
    cols = 4 * gn
    tiles = [tuple(v[t * cols : (t + 1) * cols] for v in bw) for t in range(n // cols)]
    return a, tiles


def tree(shape: str, kind: str, edges, late: str):
    m, n, g, gn = SHAPES[shape]
    sub, tiles = g * gn, n // (4 * gn)
    trees = groups(edges)
    chunks = {s: c for t in trees for s, c in slices(t).items()}
    roots = [r for t in trees for r in t if all(c != r for c, _ in edges)]
    b = Body(ledger=True)
    for t in range(tiles):
        for s in sorted(chunks):
            sweeps(b, s, 0, shape, t, chunks[s])
        for c, d in edges:
            if late == "ready":
                b.add(d, f"settle {LATE}")
            add(b, c, 0, d, sub, READY, late == "payload")
        for r in roots:
            b.add(
                r,
                "send mg[0]",
                f"    drain y{r} + {t * sub * 32} n={sub}",
                "wait mg[0]",
            )
    for r, group in zip(roots, trees, strict=True):
        others = [s for s in group if s != r]
        for q in others:
            b.ring(r, q, DONE)
            b.take(q, DONE, 1)
        b.observe(others[0], f"y{r}", observed(shape), f"seen{r}", 0)
    topo = "_".join(f"{c}{d}" for c, d in edges)
    name = f"p6_{shape}_{kind}_{topo}" + (f"_late_{late}" if late else "")
    params = [operands(shape)]
    params += [f"y{r}: {ytype(shape)}, seen{r}: f16[16] @dram" for r in roots]

    def want(ins: dict) -> dict:
        a, wt = model(ins, shape)
        ys = {r: np.zeros((m, n)) for r in roots}
        for t, w_t in enumerate(wt):
            acc = {s: partial(a, w_t, c) for s, c in chunks.items()}
            for c, d in edges:
                acc[d] = fp22(acc[d] + acc[c])
            for r in roots:
                ys[r][:, t * 4 * gn : (t + 1) * 4 * gn] = drained(acc[r])
        out = {f"y{r}": y for r, y in ys.items()}
        return out | {
            f"seen{r}": seen(y, g, gn, observed(shape)) for r, y in ys.items()
        }

    outputs = [f"{k}{r}" for r in roots for k in ("y", "seen")]
    return name, b.fn(name, ", ".join(params)), b, outputs, want


def observed(shape: str, base: int = 0) -> list[int]:
    """The u64 words an observer copies: a region's first and last two."""
    m, n, _, _ = SHAPES[shape]
    last = m * n // 4
    return [base, base + 1, base + last - 2, base + last - 1]


def seen(y: np.ndarray, g: int, gn: int, words) -> np.ndarray:
    return tiled(y, g, gn).view(np.uint64)[words].view(np.float16).astype(np.float64)


def unsplit(shape: str, kind: str, s: int):
    _, n, g, gn = SHAPES[shape]
    sub = g * gn
    b = Body()
    for t in range(n // (4 * gn)):
        sweeps(b, s, 0, shape, t, list(range(CHUNKS)))
        b.add(s, "send mg[0]", f"    drain y + {t * sub * 32} n={sub}", "wait mg[0]")
    name = f"p6_{shape}_{kind}_unsplit"

    def want(ins: dict) -> dict:
        a, wt = model(ins, shape)
        y = np.concatenate(
            [drained(partial(a, w_t, list(range(CHUNKS)))) for w_t in wt], 1
        )
        return {"y": y}

    return name, b.fn(name, f"{operands(shape)}, y: {ytype(shape)}"), b, ["y"], want


def rotating(shape: str, kind: str, sysnodes):
    """Sysnode i owns column tile i; at step j it adds tile i - j's partial."""
    _, _, g, gn = SHAPES[shape]
    sub, p = g * gn, len(sysnodes)
    chunks = slices(list(sysnodes))
    b = Body(ledger=True)
    for i, s in enumerate(sysnodes):
        sweeps(b, s, 0, shape, i, chunks[s])
    for j in range(1, p):
        for i, s in enumerate(sysnodes):
            o = sysnodes[(i - j) % p]
            sweeps(b, s, 1, shape, (i - j) % p, chunks[s])
            add(b, s, 1, o, sub, STEP + j - 1, False)
    words = sub * 4
    for i, s in enumerate(sysnodes):
        b.add(s, "send mg[0]", f"    drain y + {i * sub * 32} n={sub}", "wait mg[0]")
        for q in sysnodes:
            if q != s:
                b.ring(s, q, DONE)
    for i, s in enumerate(sysnodes):
        b.take(s, DONE, p - 1)
        t = (i + 1) % p
        at = [t * words, t * words + 1, (t + 1) * words - 2, (t + 1) * words - 1]
        b.observe(s, "y", at, "seen", 4 * i)
    name = f"p6_{shape}_{kind}_rotate"
    params = f"{operands(shape)}, y: {ytype(shape)}, seen: f16[{16 * p}] @dram"

    def want(ins: dict) -> dict:
        a, wt = model(ins, shape)
        cols = []
        for o, s in enumerate(sysnodes):
            acc = partial(a, wt[o], chunks[s])
            for j in range(1, p):
                acc = fp22(acc + partial(a, wt[o], chunks[sysnodes[(o + j) % p]]))
            cols.append(drained(acc))
        y = np.concatenate(cols, 1)
        flat = tiled(y, g, gn).view(np.uint64)
        idx = [
            w
            for i in range(p)
            for t in [(i + 1) % p]
            for w in (
                t * words,
                t * words + 1,
                (t + 1) * words - 2,
                (t + 1) * words - 1,
            )
        ]
        return {"y": y, "seen": flat[idx].view(np.float16).astype(np.float64)}

    return name, b.fn(name, params), b, ["y", "seen"], want


def vsum_image(p: int) -> str:
    """vc: `p` f16 slabs, bias and res summed in that order, TRIP words a trip."""
    ops = p + 2
    out, chain = TRIP * ops, 2 * ops
    args = ", ".join([f"p{i}" for i in range(p)] + ["bias", "res", "z"])
    bases = [f"p{i}" for i in range(p)] + ["bias", "res"]
    text = f"\nimage vsum{p}(n, {args})\n"
    text += "".join(
        f"    desc a{i} = {v} walk=(32, {TRIP})\n" for i, v in enumerate(bases)
    )
    text += f"    desc a{ops} = z walk=(32, {TRIP})\n"
    text += "".join(f"    ainc a{i} += {TRIP * 32}\n" for i in range(ops + 1))
    text += "    seti s0 = n\n    loop s0\n"
    text += "".join(f"        vfill a{i} -> l1[{TRIP * i}] rel\n" for i in range(ops))
    text += "        vsync unpack on=fill\n"
    for h in range(2):
        for i in range(ops):
            text += f"        vunpk v{2 * i + h} <- l1[{TRIP * i + 8 * h}] f16\n"
        prev = f"v{h}"
        for i in range(1, ops):
            dst = f"v{chain + 2 * (i - 1) + h}"
            text += f"        vadd {dst}, {prev}, v{2 * i + h}\n"
            prev = dst
        text += f"        vpack {prev} -> l1[{out + 8 * h}] f16\n"
    text += "        vsync drain on=pack\n"
    text += f"        vdrain a{ops} <- l1[{out}] rel\n"
    text += "        vsync fill on=drain\n        vsync pack on=drain\n"
    text += "    vsync fe on=drain\n    halt\n"
    return text


def partials(shape: str, kind: str, group, root: int):
    """The f16-partial algorithm: slabs, a fenced publication, the root's ordered
    vector sum, then DONE publishing z."""
    m, n, g, gn = SHAPES[shape]
    sub, p, slab = g * gn, len(group), m * n * 2
    chunks = slices(list(group))
    b = Body(ledger=True)
    for t in range(n // (4 * gn)):
        for k, s in enumerate(group):
            sweeps(b, s, 0, shape, t, chunks[s])
            at = k * slab + t * sub * 32
            b.add(s, "send mg[0]", f"    drain part + {at} n={sub}", "wait mg[0]")
    others = [s for s in group if s != root]
    for s in others:
        b.ring(s, root, PART)
    b.take(root, PART, p - 1)
    for k in range(p):
        b.observe(root, "part", observed(shape, k * slab // 8), "seenp", 4 * k)
    slabs = ", ".join(f"part + {k * slab}" for k in range(p))
    trips = m * n // (16 * TRIP)
    b.add(
        root,
        "send vc[0]",
        f"    run vsum{p}({trips}, {slabs}, bias, res, z)",
        "wait vc[0]",
    )
    for s in others:
        b.ring(root, s, DONE)
        b.take(s, DONE, 1)
    b.observe(others[0], "z", observed(shape), "seenz", 0)
    tag = "".join(map(str, group))
    name = f"p6_{shape}_{kind}_f16_{tag}to{root}"
    params = (
        f"{operands(shape)}, bias: {ytype(shape)}, res: {ytype(shape)}, "
        f"part: f16[{p}, {m}, {n}] @dram(tile={g}x{gn}), z: {ytype(shape, ', ieee=1')}, "
        f"seenp: f16[{16 * p}] @dram, seenz: f16[16] @dram"
    )

    def want(ins: dict) -> dict:
        a, wt = model(ins, shape)
        parts = [
            np.concatenate([drained(partial(a, w_t, chunks[s])) for w_t in wt], 1)
            for s in group
        ]
        terms = parts + [ins[k].astype(np.float64) for k in ("bias", "res")]
        z = vector_sum(terms)
        seenp = np.concatenate([seen(y, g, gn, observed(shape)) for y in parts])
        return dict(
            part=np.stack(parts),
            z=z,
            seenp=seenp,
            seenz=seen(z, g, gn, observed(shape)),
        )

    outputs = ["part", "z", "seenp", "seenz"]
    return name, b.fn(name, params), b, outputs, want


def cases(sysnodes) -> list:
    """(builder, args) of every fn."""
    have = set(sysnodes)
    topos = [e for e in TOPOLOGIES if {s for edge in e for s in edge} <= have]
    out = []
    for kind in KINDS:
        out.append((unsplit, ("t4", kind, sysnodes[0])))
        out += [(tree, ("t4", kind, e, "")) for e in (PAIR, STAR, TREE) if e in topos]
    out += [
        (tree, ("t4", "seeded", e, "")) for e in topos if e not in (PAIR, STAR, TREE)
    ]
    for shape in ("s200x192", "s100x768", "s200x768"):
        out += [(unsplit, (shape, k, sysnodes[0])) for k in ("witness", "seeded")]
        out += [(tree, (shape, "seeded", e, "")) for e in topos]
        out += [(tree, (shape, "witness", PAIR, ""))] if PAIR in topos else []
        if TREE in topos:
            out += [(tree, (shape, k, TREE, "")) for k in ("cancel", "large", "tiny")]
        if len(sysnodes) >= 2:
            pair = list(sysnodes[:2])
            out.append((partials, (shape, "seeded", pair, pair[1])))
        if len(sysnodes) == 4:
            out.append((partials, (shape, "seeded", list(sysnodes), sysnodes[0])))
    for shape in ("t4", "s200x192"):
        if PAIR in topos:
            out += [
                (tree, (shape, "seeded", PAIR, late)) for late in ("ready", "payload")
            ]
    if len(sysnodes) == 4:
        out += [(rotating, ("s200x768", k, sysnodes)) for k in ("witness", "seeded")]
        out += [
            (partials, ("s200x192", k, list(sysnodes), sysnodes[0]))
            for k in ("witness", "large", "tiny")
        ]
    return out


def generate(sysnodes: tuple[int, ...], board: str) -> Module:
    text, images, specs = "", {}, {}
    for build, args in cases(sysnodes):
        name, src, b, outputs, want = build(*args)
        shape, kind = args[0], args[1]
        m, n, _, _ = SHAPES[shape]
        given = data(kind, m, n)
        if build is partials:
            images[len(args[2])] = vsum_image(len(args[2]))
        else:
            given = {k: given[k] for k in ("x", "w")}
        text += src
        ledger = b.ledger_of() if b.ledger else None
        specs[name] = Spec(
            tuple(outputs) + (("slots",) if ledger else ()),
            want,
            numeric=() if kind == "witness" else tuple(outputs),
            inputs=lambda _, v=given: v,
            fresh=False,
            ledger=ledger,
        )
    return Module(HEAD + "".join(images.values()) + text, specs)
