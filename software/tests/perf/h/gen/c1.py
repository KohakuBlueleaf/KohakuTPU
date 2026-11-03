"""C1: the ISSUES 31 serial -> overlap -> serial triple. Three fns, each on every
sysnode with its own bindings of the same values; no block waits on another, so
the runner (`Spec.triple`) runs the same packages on the same bindings: each
sysnode of a group alone, the group together, each alone again, for every pair
of sysnodes and then all of them, a row a phase; an output whose sysnode did not
run must stay POISON. Every row carries the physical intervals (`runner.intervals`):
every span the card's memory holds after the run (queues, packages, firmware
regions, padded parameters) and each parameter's bytes and 64-byte footprint.

`vc`: the minimal vector work. Each sysnode's x is host tag words of the run's
epoch (f16 lanes, exact under unpack and pack): vc[0] copies 4 KiB of it to y
(fill, drain), vc[1] unpacks 2 KiB of it, doubles it and packs it to d, the
checked pack (doubling a tag lane moves its exponent only, so d is exact).

`mgvc`: mg[0] computes one 64 x 32 tile of a w^T (K = 256) and drains it
COUNTed into vc[1]'s L1, which writes it to q.

`chain`: a norm -> QKV -> attention -> projection-like chain, each stage reading
the one before through DRAM: h = 2 e (vc[0], the norm-like vector stage), q =
a w^T (mg[0] -> vc[1], the QKV-like matmul), s = q + h (vc[0], the
attention-like combine of both), r = a v^T (mg[0] -> vc[1], the projection-like
matmul) and z = s + r (vc[0], its residual add). Every stage's output is checked,
so the first differing stage is the first output that differs. Operands are
exact witnesses (`h.gen.p13.witness`: int7 under unit scales, every sum an
integer below 2^11; e integers in [-8, 8]): every output is compared word for
word.

Not here: the fetch / read-ahead / residency ablations (hrun configurations of
these fns, recorded in meta's knobs and boot flags) and the long stress epochs
(`--reps`). BLOCKED(execution-time fetch observation): no fixed image records the
address or words a unit actually fetched, so a transient wrong fetch is seen only
through its outputs. BLOCKED(VC-produced cluster operand): the matmul stages read
host-written MXFP7 operands; a hand-L1 vector pack into a cluster's A form is not
in these generators, so the chain's dependencies run vector -> vector and
matmul -> vector."""

import itertools

import numpy as np

from h.gen import Module, Spec
from h.gen.body import HEAD, Body, host_tags
from h.gen.p13 import take_image, witness

#: The copy's words and the pack's (a word is 32 B: 16 f16).
COPY, PACK, WORD = 128, 64, 32
#: The matmul tile: rows, K, columns, its sub-tile rows x columns, K blocks a
#: chunk, an entry's bytes; its words (128: one vector-core half).
ROWS, K, COLS, GM, GN, NK, ENTRY = 64, 256, 32, 16, 8, 2, 128
TILE = GM * GN
CHUNKS = K // (32 * NK)
#: A vector stage's registers (8 words each).
REGS = TILE // 8
#: e's integer range.
SPAN = 8

IMAGES = f"""
image cp(x, y)
    desc a0 = x walk=(32, {COPY})
    desc a1 = y walk=(32, {COPY})
    vfill a0 -> l1[0]
    vsync drain on=fill
    vdrain a1 <- l1[0]
    vsync fe on=drain
    halt

image dbl(x, y)
    desc a0 = x walk=(32, {PACK})
    desc a1 = y walk=(32, {PACK})
    vfill a0 -> l1[0]
    vsync unpack on=fill
    for j in 0..8
        vunpk v{{j}} <- l1[j*8] f16
    for j in 0..8
        vadd v{{8 + j}}, v{{j}}, v{{j}}
    for j in 0..8
        vpack v{{8 + j}} -> l1[{PACK} + j*8] f16
    vsync drain on=pack
    vdrain a1 <- l1[{PACK}]
    vsync fe on=drain
    halt

image twice(x, y)
    desc a0 = x walk=(32, {TILE})
    desc a1 = y walk=(32, {TILE})
    vfill a0 -> l1[0]
    vsync unpack on=fill
    for j in 0..{REGS}
        vunpk v{{j}} <- l1[j*8] f16
    for j in 0..{REGS}
        vadd v{{j}}, v{{j}}, v{{j}}
    for j in 0..{REGS}
        vpack v{{j}} -> l1[{2 * TILE} + j*8] f16
    vsync drain on=pack
    vdrain a1 <- l1[{2 * TILE}]
    vsync fe on=drain
    halt

image sum(p, q, y)
    desc a0 = p walk=(32, {TILE})
    desc a1 = q walk=(32, {TILE})
    desc a2 = y walk=(32, {TILE})
    vfill a0 -> l1[0]
    vfill a1 -> l1[{TILE}]
    vsync unpack on=fill
    for j in 0..{REGS}
        vunpk v{{j}} <- l1[j*8] f16
    for j in 0..{REGS}
        vunpk v{{{REGS} + j}} <- l1[{TILE} + j*8] f16
    for j in 0..{REGS}
        vadd v{{j}}, v{{j}}, v{{{REGS} + j}}
    for j in 0..{REGS}
        vpack v{{j}} -> l1[{2 * TILE} + j*8] f16
    vsync drain on=pack
    vdrain a2 <- l1[{2 * TILE}]
    vsync fe on=drain
    halt
"""
TAKE, TAKE_TEXT = take_image(TILE, 0)


def gemm(b: Body, n: int, a: str, w: str, out: str) -> None:
    """n's mg[0] tile a w^T drained COUNTed into vc[1], which writes it to `out`."""
    a_chunk, b_chunk = GM * NK * ENTRY, GN * NK * ENTRY
    lines = ["send mg[0]"]
    for c in range(CHUNKS):
        lines += [
            f"    fill A{c % 2} <- {a} + {c * a_chunk} n={GM * NK}",
            f"    fill B{c % 2} <- {w} + {c * b_chunk} n={GN * NK}",
            f"    gemm {GM}x{GN}x{NK} a=A{c % 2} b=B{c % 2} acc={int(c > 0)}",
        ]
    lines += [f"    drain n={TILE} to=vc[1] l1=0 count", "send vc[1]"]
    lines += [f"    run {TAKE}({out})", "wait vc[1]", "wait mg[0]"]
    b.add(n, *lines)


def vector(b: Body, n: int, run: str) -> None:
    b.add(n, "send vc[0]", f"    run {run}", "wait vc[0]")


def matmul_params(n: int, weights: str) -> list[str]:
    out = [f"a{n}: mx7a[{ROWS}, {K}] @dram(gm={GM}, nk={NK})"]
    return out + [f"{w}{n}: mx7b[{COLS}, {K}] @dram(gn={GN}, nk={NK})" for w in weights]


def tile(n: int, name: str) -> str:
    return f"{name}{n}: f16[{ROWS}, {COLS}] @dram(tile={GM}x{GN})"


def operands(sysnodes, weights: str, e: bool):
    """The same witness values for every sysnode's own a, weights (and e)."""
    rng = np.random.default_rng(5)
    a = witness(ROWS, K, 0, 1)
    ws = {w: witness(COLS, K, 1, 2 + i) for i, w in enumerate(weights)}
    ev = rng.integers(-SPAN, SPAN + 1, (ROWS, COLS)).astype(np.float16)
    out = {}
    for n in sysnodes:
        out[f"a{n}"] = a
        out |= {f"{w}{n}": v for w, v in ws.items()}
        if e:
            out[f"e{n}"] = ev
    return out


def product(ins: dict, n: int, w: str) -> np.ndarray:
    return ins[f"a{n}"].astype(np.float64) @ ins[f"{w}{n}"].astype(np.float64).T


def vc_fn(sysnodes):
    b = Body()
    params = []
    for n in sysnodes:
        b.add(n, "send vc[0]", f"    run cp(x{n}, y{n})")
        b.add(n, "send vc[1]", f"    run dbl(x{n}, d{n})", "wait vc[0]", "wait vc[1]")
        params += [
            f"x{n}: f16[{COPY * WORD // 2}] @dram",
            f"y{n}: f16[{COPY * WORD // 2}] @dram",
            f"d{n}: f16[{PACK * WORD // 2}] @dram",
        ]

    def want(ins: dict) -> dict:
        out = {}
        for n in sysnodes:
            x = ins[f"x{n}"]
            out[f"y{n}"] = x
            out[f"d{n}"] = x[: PACK * WORD // 2] * np.float16(2)
        return out

    outs = {f"{k}{n}": n for n in sysnodes for k in "yd"}
    tags = host_tags({f"x{n}": (COPY * WORD, np.float16, n) for n in sysnodes})
    return b, ", ".join(params), outs, want, tags, True


def mgvc_fn(sysnodes):
    b = Body()
    params = []
    for n in sysnodes:
        gemm(b, n, f"a{n}", f"w{n}", f"q{n}")
        params += [*matmul_params(n, "w"), tile(n, "q")]

    def want(ins: dict) -> dict:
        return {f"q{n}": product(ins, n, "w") for n in sysnodes}

    given = operands(sysnodes, "w", False)
    owners = {f"q{n}": n for n in sysnodes}
    return b, ", ".join(params), owners, want, lambda _: given, False


def chain_fn(sysnodes):
    b = Body()
    params = []
    for n in sysnodes:
        vector(b, n, f"twice(e{n}, h{n})")
        gemm(b, n, f"a{n}", f"w{n}", f"q{n}")
        vector(b, n, f"sum(q{n}, h{n}, s{n})")
        gemm(b, n, f"a{n}", f"v{n}", f"r{n}")
        vector(b, n, f"sum(s{n}, r{n}, z{n})")
        params += [*matmul_params(n, "wv"), *(tile(n, t) for t in "ehqsrz")]

    def want(ins: dict) -> dict:
        out = {}
        for n in sysnodes:
            h = 2 * ins[f"e{n}"].astype(np.float64)
            q, r = product(ins, n, "w"), product(ins, n, "v")
            s = q + h
            out |= {f"h{n}": h, f"q{n}": q, f"s{n}": s, f"r{n}": r, f"z{n}": s + r}
        return out

    outs = {f"{t}{n}": n for n in sysnodes for t in "hqsrz"}
    given = operands(sysnodes, "wv", True)
    return b, ", ".join(params), outs, want, lambda _: given, False


def generate(sysnodes: tuple[int, ...], board: str) -> Module:
    pairs = list(itertools.combinations(sysnodes, 2))
    overlaps = tuple(dict.fromkeys([*pairs, tuple(sysnodes)]))
    text, specs = HEAD + IMAGES + TAKE_TEXT, {}
    tag = "".join(map(str, sysnodes))
    for kind, build in (("vc", vc_fn), ("mgvc", mgvc_fn), ("chain", chain_fn)):
        b, params, owners, want, inputs, fresh = build(sysnodes)
        name = f"c1_{kind}_{tag}"
        text += b.fn(name, params)
        specs[name] = Spec(
            tuple(owners),
            want,
            inputs=inputs,
            fresh=fresh,
            triple=True,
            overlaps=overlaps,
            owners=owners,
        )
    return Module(text, specs)
