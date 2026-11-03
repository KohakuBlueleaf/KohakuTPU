"""P13: local epilogue routes, fill multicast and the staging ports, on the first
sysnode alone. Every product is an exact witness: each 32-wide K block of a row of
x holds 63 at k 0 and of w at k 1 (MXFP7 scale 1, so the int7 is the value), every
other element -1, 0 or 1, zero where the other side holds its 63; y = x w^T is
then integers below 2^11, exact through MXFP7, FP22 and f16. x, w are written once.

Epilogue: one 64 x 256 product (K = 256) of tiles on 1, 2 or 4 clusters (4 only
where the board has them), cluster u feeding vc[u % 2]. A tile goes cluster ->
core directly (a COUNTed drain into the core's L1, the core writing it to y), or
cluster -> staging -> core (a plain drain into a @stage buffer, the core copying
it to y once the cluster's drain retired). One buffer (16 x 16 sub-tile tiles, one
256-word receive slot a core) or ping-pong (16 x 8 tiles, two 128-word slots): the
same receive capacity. A slot is reused only after the core's run that read it
(a mark waited), and a second cluster's burst into a core only after the first
cluster's burst landed there (its take waited): legal serialized receive leases.
With 2 or 4 clusters, x's FILL A is shared (`peers=`, follower fills, a mark, the
leader's fill after it, as examples/l1/multinode/matmul2.ktpu) or not.

B entries: two clusters, one 16-row tile each of the same w columns, B fills of
63, 64 or 65 entries (nk = 1) shared or not. 65 entries exceed `mg.shared_b_max`
(64), and a whole shared 65-entry fill is refused at compile ("shared B FILL
exceeds mg.shared_b_max"), so its shared form is split: 64 shared entries and one
entry each cluster fills alone.

Staging ports: vc[0] and vc[1] at once, each 4 trips of 64 staging entries (4
words each, 512 B apart: one bank), read (fills, the last trip written to v) or
written (one fill from x, then drains into a @stage output): read/read,
write/write, read/write, both on the same bank phase or the second one entry
(128 B, the next bank) on; and read/read, write/write with vc[1] on DRAM, the two
memory ports' competition. The phase is relative to each buffer's bound base.

Not here: the fill / compute / drain subphases (no counters: busy and cycles only).
BLOCKED(requested-read and NoC delivery counters): no fixed image counts the reads
a shared fill saves or the flits delivered, so the saving is the cycle and busy
difference of the shared and unshared fns, not a counted read."""

import itertools
import json

import numpy as np

from h import check
from h.gen import Module, Spec
from h.gen.body import HEAD, Body, poison
from kohakutpu.application.runtime.card.common import MACHINES

#: The epilogue product: rows, K, columns; a sub-tile's bytes, an entry's.
ROWS, K, COLS = 64, 256, 256
SUBTILE, ENTRY = 32, 128
#: K blocks a chunk (nk) of the epilogue; its chunks.
NK, CHUNKS = 2, K // 64
#: (sub-tile rows, sub-tile columns) of a tile, by receive buffers a core.
TILES = {1: (16, 16), 2: (16, 8)}
#: The B-entry cases: K, rows a cluster.
B_K, B_ROWS = 128, 16
B_ENTRIES = (63, 64, 65)
SHARED_B_MAX = 64
#: The staging streams: entries a trip, trips, an entry's stride (one bank).
TRIP, TRIPS, STRIDE = 64, 4, 512

IMAGES = f"""
image cp(n, s, y)
    desc a0 = s walk=(32, n)
    desc a1 = y walk=(32, n)
    vfill a0 -> l1[0]
    vsync drain on=fill
    vdrain a1 <- l1[0]
    vsync fe on=drain
    halt

image rd(n, src, v)
    desc a0 = src walk=((32, 4), ({STRIDE}, {TRIP}))
    desc a1 = v walk=(32, {4 * TRIP})
    ainc a0 += {STRIDE * TRIP}
    seti s0 = n
    loop s0
        vfill a0 -> l1[0] rel
        vsync fe on=fill
    vdrain a1 <- l1[0]
    vsync fe on=drain
    halt

image wr(n, x, dst)
    desc a0 = x walk=(32, {4 * TRIP})
    desc a1 = dst walk=((32, 4), ({STRIDE}, {TRIP}))
    ainc a1 += {STRIDE * TRIP}
    vfill a0 -> l1[0]
    vsync drain on=fill
    seti s0 = n
    loop s0
        vdrain a1 <- l1[0] rel
    vsync fe on=drain
    halt
"""


def take_image(n: int, at: int) -> tuple[str, str]:
    """A core taking `n` COUNTed words at L1 word `at` and writing them to y."""
    name = f"take_{n}_{at}"
    return name, (
        f"\nimage {name}(y)\n    desc a0 = y walk=(32, {n})\n"
        f"    vwait drain rx n={n}\n    vdrain a0 <- l1[{at}]\n"
        "    vsync fe on=drain\n    halt\n"
    )


def witness(rows: int, k: int, peak: int, seed: int) -> np.ndarray:
    """An exact MXFP7 operand: 63 at k `peak` of every block, zero at the other
    side's peak, -1 / 0 / 1 elsewhere."""
    rng = np.random.default_rng(seed)
    a = rng.integers(-1, 2, (rows, k)).astype(np.float16)
    a[:, (1 - peak) :: 32] = 0
    a[:, peak::32] = 63
    return a


def operands(m: int, n: int, k: int):
    def inputs(_: int) -> dict:
        return {"x": witness(m, k, 0, 1), "w": witness(n, k, 1, 2)}

    return inputs


def product(ins: dict) -> dict:
    x, w = (ins[p].astype(np.float64) for p in "xw")
    return {"y": x @ w.T}


def mgs_of(board: str) -> int:
    return json.loads((MACHINES / f"{board}.json").read_text("utf-8"))["machine"]["mg"][
        "count"
    ]


def pairs(units: int) -> list[tuple[int, int]]:
    """(leader, follower) clusters of a shared fill: one of each half."""
    half = units // 2
    return [(u, u + half) for u in range(half)]


def gemms(b: Body, n: int, tiles: list[tuple[int, int]], gm: int, gn: int, share):
    """Each (cluster, column tile) computed, x's fills shared between `share`'s
    pairs; returns nothing waited."""
    a_chunk, b_chunk = gm * NK * ENTRY, gn * NK * ENTRY
    tile_of = dict(tiles)

    def own(u: int, c: int, a_peer: str) -> list[str]:
        j = tile_of[u]
        return [
            f"    fill A{c % 2} <- x + {c * a_chunk} n={gm * NK}{a_peer}",
            f"    fill B{c % 2} <- w + {(j * CHUNKS + c) * b_chunk} n={gn * NK}",
            f"    gemm {gm}x{gn}x{NK} a=A{c % 2} b=B{c % 2} acc={int(c > 0)}",
        ]

    if not share:
        for u, _ in tiles:
            b.add(n, f"send mg[{u}]")
            for c in range(CHUNKS):
                b.add(n, *own(u, c, ""))
        return
    for (lead, follow), c in itertools.product(pairs(len(tiles)), range(CHUNKS)):
        j = tile_of[follow]
        b.add(
            n,
            f"send mg[{follow}]",
            f"    fill B{c % 2} <- w + {(j * CHUNKS + c) * b_chunk} n={gn * NK}",
            f"t{follow} = mark mg[{follow}]",
            f"send mg[{follow}]",
            f"    fill A{c % 2} <- x + {c * a_chunk} n={gm * NK} peers=(mg[{lead}])",
            f"    gemm {gm}x{gn}x{NK} a=A{c % 2} b=B{c % 2} acc={int(c > 0)}",
            f"wait mg[{follow}] upto=t{follow}",
            f"send mg[{lead}]",
        )
        lines = own(lead, c, f" peers=(mg[{follow}])")
        b.add(n, lines[0], lines[1], lines[2])


def epilogue(n: int, units: int, path: str, bufs: int, share: bool, images: dict):
    gm, gn = TILES[bufs]
    sub = gm * gn
    total = COLS // (4 * gn)
    per = total // units
    b = Body()
    #: Per core: tiles it took, each slot's last reading run, its last sender.
    taken: dict = {0: 0, 1: 0}
    slot_mark: dict = {}
    last_in: dict = {}
    for r in range(per):
        tiles = [(u, u * per + r) for u in range(units)]
        gemms(b, n, tiles, gm, gn, share)
        drained = []
        for u, j in tiles:
            q = u % 2
            slot = taken[q] % bufs
            taken[q] += 1
            held = [slot_mark.get((q, slot))]
            if path == "direct" and q in last_in and last_in[q][0] != u:
                held.append(last_in[q][1])
            for t in dict.fromkeys(m for m in held if m):
                b.add(n, f"wait vc[{q}] upto={t}")
            at, mark = slot * sub, f"v{q}_{taken[q]}"
            if path == "direct":
                take, text = take_image(sub, at)
                images[take] = text
                b.add(
                    n,
                    f"send mg[{u}]",
                    f"    drain n={sub} to=vc[{q}] l1={at} count",
                    f"send vc[{q}]",
                    f"    run {take}(y + {j * sub * SUBTILE})",
                    f"{mark} = mark vc[{q}]",
                )
                slot_mark[q, slot] = mark
                last_in[q] = (u, mark)
                continue
            s = (u * bufs + slot) * sub * SUBTILE
            b.add(
                n, f"send mg[{u}]", f"    drain s + {s} n={sub}", f"d{u} = mark mg[{u}]"
            )
            drained.append((u, q, slot, j, s, mark))
        for u, q, slot, j, s, mark in drained:
            b.add(
                n,
                f"wait mg[{u}] upto=d{u}",
                f"send vc[{q}]",
                f"    run cp({sub}, s + {s}, y + {j * sub * SUBTILE})",
                f"{mark} = mark vc[{q}]",
            )
            slot_mark[q, slot] = mark
    b.add(n, "barrier")
    form = "shared" if share else "own"
    name = f"p13_{path}_mg{units}_buf{bufs}_{form}"
    params = (
        f"x: mx7a[{ROWS}, {K}] @dram(gm={gm}, nk={NK}), "
        f"w: mx7b[{COLS}, {K}] @dram(gn={gn}, nk={NK}), "
        f"y: f16[{ROWS}, {COLS}] @dram(tile={gm}x{gn})"
    )
    if path == "stage":
        params += f", s: f16[{units * bufs * sub * 16}] @stage(node={n})"
    return name, b.fn(name, params)


def b_entries(n: int, entries: int, shared: bool):
    """Clusters 0 and 1, row tiles 0 and 1 of x, the same w columns."""
    gm, chunks = B_ROWS // 4, B_K // 32
    b = Body()
    split = shared and entries > SHARED_B_MAX
    head = SHARED_B_MAX if split else entries

    def own_a(u: int, c: int) -> str:
        return f"    fill A{c % 2} <- x + {(u * chunks + c) * gm * ENTRY} n={gm}"

    def b_fill(c: int, count: int, first: int, peer: str) -> str:
        at = f" at={first}" if first else ""
        src = (c * entries + first) * ENTRY
        return f"    fill B{c % 2} <- w + {src} n={count}{at}{peer}"

    gemm = [
        f"    gemm {gm}x{entries}x1 a=A{c % 2} b=B{c % 2} acc={int(c > 0)}"
        for c in range(chunks)
    ]
    for c in range(chunks):
        if not shared:
            for u in (0, 1):
                b.add(
                    n, f"send mg[{u}]", own_a(u, c), b_fill(c, entries, 0, ""), gemm[c]
                )
            continue
        b.add(n, "send mg[1]", own_a(1, c))
        if split:
            b.add(n, b_fill(c, entries - head, head, ""))
        b.add(
            n,
            "t1 = mark mg[1]",
            "send mg[1]",
            b_fill(c, head, 0, " peers=(mg[0])"),
            gemm[c],
            "wait mg[1] upto=t1",
            "send mg[0]",
            b_fill(c, head, 0, " peers=(mg[1])"),
        )
        if split:
            b.add(n, b_fill(c, entries - head, head, ""))
        b.add(n, own_a(0, c), gemm[c])
    sub = gm * entries
    for u in (0, 1):
        b.add(n, f"send mg[{u}]", f"    drain y + {u * sub * SUBTILE} n={sub}")
    b.add(n, "barrier")
    form = ("split" if split else "shared") if shared else "own"
    name = f"p13_b{entries}_{form}"
    params = (
        f"x: mx7a[{2 * B_ROWS}, {B_K}] @dram(gm={gm}, nk=1), "
        f"w: mx7b[{4 * entries}, {B_K}] @dram(gn={entries}, nk=1), "
        f"y: f16[{2 * B_ROWS}, {4 * entries}] @dram(tile={gm}x{entries})"
    )
    return name, b.fn(name, params)


def ports(n: int, mix: str, phase: int, dram: bool):
    """vc[0] and vc[1]'s streams, `mix` r (read) / w (write) each; `phase`: the
    second stream's entry offset; `dram`: vc[1] on DRAM."""
    b = Body()
    span = TRIPS * TRIP * STRIDE
    params, outs = [f"x: f16[{4 * TRIP * 16}] @dram"], {}
    for q, kind in enumerate(mix):
        space = "dram" if dram and q else f"stage(node={n})"
        off = phase * ENTRY if q else 0
        buf = f"s{q}"
        params.append(f"{buf}: f16[{(off + span) // 2}] @{space}")
        if kind == "r":
            params.append(f"v{q}: f16[{4 * TRIP * 16}] @dram")
            b.add(n, f"send vc[{q}]", f"    run rd({TRIPS}, {buf} + {off}, v{q})")
        else:
            b.add(n, f"send vc[{q}]", f"    run wr({TRIPS}, x, {buf} + {off})")
        outs[q] = (kind, off)
    b.add(n, "wait vc[0]", "wait vc[1]")
    where = "dram" if dram else ("bank1" if phase else "bank0")
    name = f"p13_ports_{mix}_{where}"
    return name, b.fn(name, ", ".join(params)), outs


def stage_spec(outs: dict) -> Spec:
    reads = [q for q, (kind, _) in outs.items() if kind == "r"]
    writes = [q for q, (kind, _) in outs.items() if kind == "w"]
    span = TRIPS * TRIP * STRIDE

    def tagged(_: int) -> dict:
        got = {"x": check.tagged(0, check.HOST, 0, 4 * TRIP * 32, np.float16)}
        for q in reads:
            nbytes = outs[q][1] + span
            got[f"s{q}"] = check.tagged(0, check.HOST, 1 + q, nbytes, np.float16)
        return got

    def want(ins: dict) -> dict:
        got = {}
        for q in reads:
            off = outs[q][1]
            s = ins[f"s{q}"][off // 2 :].reshape(TRIPS, TRIP, STRIDE // 2)
            got[f"v{q}"] = s[-1, :, : ENTRY // 2].reshape(-1)
        for q in writes:
            off = outs[q][1]
            body = poison(np.float16, span // 2).reshape(TRIPS * TRIP, STRIDE // 2)
            body[:, : ENTRY // 2] = np.tile(
                ins["x"].reshape(TRIP, ENTRY // 2), (TRIPS, 1)
            )
            got[f"s{q}"] = np.concatenate([poison(np.float16, off // 2), body.ravel()])
        return got

    outputs = tuple(f"v{q}" for q in reads) + tuple(f"s{q}" for q in writes)
    return Spec(outputs, want, inputs=tagged, fresh=False)


def generate(sysnodes: tuple[int, ...], board: str) -> Module:
    n = sysnodes[0]
    units = [u for u in (1, 2, 4) if u <= mgs_of(board)]
    text, images, specs = "", {}, {}
    for path, u, bufs in itertools.product(("direct", "stage"), units, (1, 2)):
        for share in (False, True) if u > 1 else (False,):
            name, src = epilogue(n, u, path, bufs, share, images)
            text += src
            specs[name] = Spec(
                ("y",), product, inputs=operands(ROWS, COLS, K), fresh=False
            )
    for entries, shared in itertools.product(B_ENTRIES, (False, True)):
        name, src = b_entries(n, entries, shared)
        text += src
        specs[name] = Spec(
            ("y",), product, inputs=operands(2 * B_ROWS, 4 * entries, B_K), fresh=False
        )
    cases = [(m, p, False) for m in ("rr", "ww", "rw") for p in (0, 1)]
    cases += [(m, 0, True) for m in ("rr", "ww")]
    for mix, phase, dram in cases:
        name, src, outs = ports(n, mix, phase, dram)
        text += src
        specs[name] = stage_spec(outs)
    return Module(HEAD + IMAGES + "".join(images.values()) + text, specs)
