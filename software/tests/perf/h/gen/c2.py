"""C2: shared-tensor writes, the ISSUES 79 reproduction and its one-factor
variants. After a start barrier (two or more writers, with a token ledger) every
writer writes its shard into one DRAM tensor y: row r of writer q's shard (`width`
bytes) at y + r * pitch + offset(q); nothing reads y on the card, the host reads
it back after the run. Every word of y is checked: the rest of y must stay
POISON, and every differing 64-byte line is recorded with the offset where its
bytes belong (`check.misplaced`), so a misplaced line names both addresses (the
row's `binds` gives y's base).

Shard q of x is tag words of producer s (the writer's sysnode), word o of the
shard tagged region o >> 20, offset o mod 2^20: a misplaced line decodes to
(producer, region << 20 | offset), unaliased up to 512 MiB a shard. x is written
once; y is painted POISON every run.

Base: four vector-core writers (P12's `put`, a core's fill then a pitched drain,
8 KiB a burst), 8 KiB rows, 512 rows (16 MiB of writes, beyond the 8 MiB aggregate
cache), packed (P12's layout: a row of y is every writer's run side by side). The
reproductions are P12's two failing shapes, writes only: 8192 x 8192 and
1024 x 4096. One factor at a time from the base:
- writer agent: `mg` (cluster drains of 8 KiB tiles a w_t^T, a distinct tile a row:
  exact witness products, misplaced lines found by content, not by tag) and
  `mover` (one copy a row);
- row width (a writer's contiguous chunk) 4, 16, 32 and 64 KiB at 16 MiB;
- one or two writers instead of four;
- stripes: `adjacent` (writer q's rows on 16 KiB stripe q of each 64 KiB: each
  writer on its own interleave home, neighbours adjacent) and `same` (every row of
  every writer on stripe 0: one home);
- cache-resident: 64 rows (2 MiB).

The sweep is `--reps` (P12 manifested in 1/20 to 7/20 runs); every fn is
independent. Not here: staggered issue.

ISSUES 79's provenance cut (Astra, `h_line` H79-04): `c2_i79_<pieces>_<fresh|stamp>`
is P12's highest-incidence fn whole (8192 x 8192, four producer sysnodes each on
its own vc[0], start barrier, then sysnodes 0 and 1 reading their rows of y, the
last tile to v), canonical addresses and P12's bindings (x, y, v; no ledger).
x is injective tag words (offset bits 20.. in the region field), its epoch the
run's (mod 1024), rewritten every run. `stamp`: writer q multiplies the tile in
L1 by 2^DELTAS[q] between fill and drain (`h.stamp`), so a wrong word names its
producer, offset, epoch and the executing writer; the row's `provenance` decodes
every retained mismatch of y and v, of the first read and of the quiet reread.
`pieces`: a tile's fill as separate fills into consecutive L1 words, the drain
unchanged. Their 64-byte reads (`reads`, the tile 8 KiB aligned; the core caps
a run at 255 words, a memory request at 16 words and a 4 KiB page):

| pieces | the last fill's reads | one-beat read |
|---|---|---|
| 256 (core runs 255 + 1) | 32 B at 8160: beat 8128 | yes, upper half |
| 128 + 128 | 8 x 512 B | none |
| 255 + 1 (the 255-word chunk, 1-word tail) | 32 B at 8160 | yes, upper half |
| 254 + 2 | 64 B at 8128 | yes, whole |
| 253 + 3 | 96 B at 8096: beats 8064, 8128 | none |
| 252 + 4 | 128 B at 8064: two beats | none |

Outcomes, per mismatched word (`stamp.Gather.kind`), each against its reread:
- `before_victim_transform` (victim's stamp on foreign data), reread stable:
  the foreign data was in L1 before the victim's transform: read side, or x
  itself (x is not reread; its bindings and epoch are in the row).
- `after_donor_transform` (the donor's own stamp), reread stable: substitution
  after the donor's transform (L1, NoC, write path or a write address).
- `raw_input_or_missing_transform` / `other_writer` / `invalid_or_mixed`: no
  single cut; the raw words are kept.
- the reread differs from the first read (`reread.changed`): the observation
  path is involved; the first read alone does not show memory.
- `fresh` manifesting while `stamp` does not: inconclusive (the transform
  perturbs timing), not a repair. 128 + 128 clean while 256 manifests: the
  one-beat fill tail is implicated; 253 + 3 and 252 + 4 clean while 255 + 1 and
  254 + 2 manifest: the trigger is a one-beat read, not the piece count."""

import dataclasses
import functools
import itertools

import numpy as np

from h import check, stamp
from h.gen import Module, Spec
from h.gen.body import HEAD, Body, poison
from h.gen.p12 import DONE, get_image, put_image
from h.gen.p13 import witness
from kohakutpu.application.runtime.card.common import MACHINES
from kohakutpu.stack.buffer import Buffer

KB, MB = 1 << 10, 1 << 20
#: A core's burst at most (half its L1), the interleave stripe and its period.
BURST, STRIPE, PERIOD = 8 * KB, 16 * KB, 64 * KB
#: The beyond-cache writes' bytes, and the tag offset field's words.
TOTAL, FIELD_WORDS = 16 * MB, 1 << 20
#: The cluster tile: 64 x 64 f16 (8 KiB), K = 64; an entry's bytes.
EDGE, GM, NK, ENTRY = 64, 16, 2, 128
B_TILE = GM * NK * ENTRY
#: A descriptor increment's bound (signed 18 bits, L1.IMG.047).
AINC = 1 << 17
START = 1
LAYOUT_BOARD = "multimesh_v9t6"
#: ISSUES 79's shape: rows, a row's bytes, readers; a tile's words (one row).
I79_ROWS, I79_WIDTH, I79_READERS = 8192, 8 * KB, 2
TILE_WORDS, WORD = 256, 32
#: A core read run's words at most, a memory request's, its page, a beat's bytes.
RUN_WORDS, REQUEST_WORDS, PAGE, BEAT = 255, 16, 4 * KB, 64
#: Each tile's fill pieces (words), and the registers a stamp group uses.
PIECES = ((256,), (128, 128), (255, 1), (254, 2), (253, 3), (252, 4))
GROUP = 16


@dataclasses.dataclass(frozen=True)
class Case:
    agent: str
    width: int
    rows: int
    writers: int
    place: str

    def pitch(self, p: int) -> int:
        return {"packed": p * self.width, "adjacent": PERIOD, "same": p * PERIOD}[
            self.place
        ]

    def offset(self, q: int) -> int:
        return {"packed": q * self.width, "adjacent": q * STRIPE, "same": q * PERIOD}[
            self.place
        ]

    @property
    def name(self) -> str:
        return (
            f"c2_{self.agent}_w{self.width >> 10}k_r{self.rows}"
            f"_n{self.writers}_{self.place}"
        )


def shard(s: int, nbytes: int) -> np.ndarray:
    """Producer s's tag words, the offset's high bits in the region field."""
    words = nbytes // 8
    return np.concatenate(
        [
            check.tags(0, s, k, np.arange(lo, min(lo + FIELD_WORDS, words)))
            for k, lo in enumerate(range(0, words, FIELD_WORDS))
        ]
    )


def vc_lines(c: Case, q: int, pitch: int, images: dict) -> list[str]:
    """One pitched run of whole-row bursts, or a run a row where a row is past a
    burst or a trip's step past `AINC`."""
    src, dst = q * c.rows * c.width, c.offset(q)
    per = max(
        [d for d in range(1, c.rows + 1) if c.rows % d == 0 and d * c.width <= BURST],
        default=0,
    )
    if per and per * pitch < AINC:
        name, text = put_image(c.width, per, pitch)
        images[name] = text
        runs = [f"    run {name}({c.rows // per}, x + {src}, y + {dst})"]
    else:
        chunk = min(c.width, BURST)
        name, text = put_image(chunk, 1, chunk)
        images[name] = text
        runs = [
            f"    run {name}({c.width // chunk}, x + {src + r * c.width}, "
            f"y + {dst + r * pitch})"
            for r in range(c.rows)
        ]
    return ["send vc[0]", *runs, "wait vc[0]"]


def mover_lines(c: Case, q: int, pitch: int) -> list[str]:
    src, dst, n = q * c.rows * c.width, c.offset(q), c.width // 32
    return [
        "move",
        *(
            f"    copy w(x + {src + r * c.width}, ({n}, 32)) -> "
            f"w(y + {dst + r * pitch}, ({n}, 32))"
            for r in range(c.rows)
        ),
    ]


def mg_lines(c: Case, s: int, q: int, pitch: int) -> list[str]:
    return [
        "send mg[0]",
        f"    fill A0 <- a + 0 n={GM * NK}",
        "r3 = set 0",
        f"r4 = set {c.rows}",
        "label trip",
        f"r5 = mul r3, {B_TILE}",
        f"r6 = mul r3, {pitch}",
        "send mg[0]",
        f"    fill B0 <- w{s} + 0 + r5 n={GM * NK}",
        f"    gemm {GM}x{GM}x{NK} a=A0 b=B0 acc=0",
        f"    drain y + {c.offset(q)} + r6 n={GM * GM}",
        "r3 = add r3, 1",
        "goto trip if r3 < r4",
        "wait mg[0]",
    ]


def tiles(a: np.ndarray, w: np.ndarray, rows: int) -> np.ndarray:
    """Row t's 8 KiB: the tile a w_t^T in the drain's layout, as f16."""
    json = (MACHINES / f"{LAYOUT_BOARD}.json").read_text("utf-8")
    y = a.astype(np.float64) @ w.astype(np.float64).T
    stacked = y.reshape(EDGE, rows, EDGE).transpose(1, 0, 2).reshape(-1, EDGE)
    buf = Buffer.of(f"f16[{rows * EDGE}, {EDGE}] @dram(tile={GM}x{GM})", json)
    return np.frombuffer(buf.pack(stacked), np.float16)


def write(sysnodes, c: Case, images: dict):
    """Returns the fn's text and Spec."""
    p = len(sysnodes)
    pitch = c.pitch(p)
    writers = list(sysnodes[: c.writers])
    b = Body(ledger=len(writers) > 1)
    if len(writers) > 1:
        b.barrier(writers, START)
    for q, s in enumerate(writers):
        if c.agent == "vc":
            b.add(s, *vc_lines(c, q, pitch, images))
        if c.agent == "mover":
            b.add(s, *mover_lines(c, q, pitch))
        if c.agent == "mg":
            b.add(s, *mg_lines(c, s, q, pitch))
    shard_bytes = c.rows * c.width
    params = f"y: f16[{c.rows * pitch // 2}] @dram"
    if c.agent == "mg":
        params += f", a: mx7a[{EDGE}, {EDGE}] @dram(gm={GM}, nk={NK})"
        params += "".join(
            f", w{s}: mx7b[{EDGE * c.rows}, {EDGE}] @dram(gn={GM}, nk={NK})"
            for s in writers
        )
        given = {"a": witness(EDGE, EDGE, 0, 1)}
        given |= {f"w{s}": witness(EDGE * c.rows, EDGE, 1, 10 + s) for s in writers}
    else:
        params += f", x: f16[{len(writers) * shard_bytes // 2}] @dram"
        given = {
            "x": np.concatenate([shard(s, shard_bytes) for s in writers]).view(
                np.float16
            )
        }

    def want(ins: dict) -> dict:
        y = poison(np.float16, c.rows * pitch // 2).reshape(c.rows, pitch // 2)
        for q, s in enumerate(writers):
            if c.agent == "mg":
                rows = tiles(ins["a"], ins[f"w{s}"], c.rows)
            else:
                rows = ins["x"][q * shard_bytes // 2 : (q + 1) * shard_bytes // 2]
            lo = c.offset(q) // 2
            y[:, lo : lo + c.width // 2] = rows.reshape(c.rows, c.width // 2)
        return {"y": y.reshape(-1)}

    spec = Spec(
        ("y",) + (("slots",) if b.ledger else ()),
        want,
        inputs=lambda _: given,
        fresh=False,
        ledger=b.ledger_of() if b.ledger else None,
    )
    return b.fn(c.name, params), spec


def reads(pieces) -> list[list[tuple[int, int]]]:
    """Each fill piece's memory reads in an 8 KiB-aligned tile, as (the first
    64-byte beat's byte offset, beats)."""
    out, at = [], 0
    for n in pieces:
        end, got = at + n * WORD, []
        for run in range(at, end, RUN_WORDS * WORD):
            lo, stop = run, min(run + RUN_WORDS * WORD, end)
            while lo < stop:
                hi = min(lo + REQUEST_WORDS * WORD, stop, (lo // PAGE + 1) * PAGE)
                got.append((lo // BEAT * BEAT, -(-hi // BEAT) - lo // BEAT))
                lo = hi
        out.append(got)
        at = end
    return out


def i79_image(pieces, pitch: int, writer: int | None) -> tuple[str, str]:
    """P12's put of one row a trip into rows `pitch` apart, its fill issued as
    `pieces`; `writer` set: its stamp applied to the tile in L1 before the drain."""
    stamped = "" if writer is None else f"_w{writer}"
    name = f"i79_{'_'.join(map(str, pieces))}_{pitch}{stamped}"
    # a1 is the drain's, as in P12's put; piece i fills from a0, a2, a3, ...
    descs = [0, *range(2, len(pieces) + 1)]
    srcs = [f"src{i}" for i in range(len(pieces))]
    starts = list(itertools.accumulate(pieces, initial=0))[:-1]
    lines = [
        f"image {name}(n, {', '.join(srcs)}, dst)",
        f"    desc a0 = src0 walk=(32, {pieces[0]})",
        f"    desc a1 = dst walk=((32, {TILE_WORDS}), ({pitch}, 1))",
        *(
            f"    desc a{d} = {s} walk=(32, {k})"
            for d, s, k in zip(descs[1:], srcs[1:], pieces[1:], strict=True)
        ),
        f"    ainc a0 += {TILE_WORDS * WORD}",
        f"    ainc a1 += {pitch}",
        *(f"    ainc a{d} += {TILE_WORDS * WORD}" for d in descs[1:]),
        "    seti s0 = n",
    ]
    if writer is not None:
        lines += [f"    seti s1 = {float(2 ** stamp.DELTAS[writer])}"]
    lines += ["    loop s0"]
    lines += [
        f"        vfill a{d} -> l1[{a}] rel" for d, a in zip(descs, starts, strict=True)
    ]
    if writer is None:
        lines += ["        vsync drain on=fill"]
    else:
        lines += ["        vsync unpack on=fill"]
        for g in range(0, TILE_WORDS, 8 * GROUP):
            # Pack -> unpack: the next group reuses the registers.
            if g:
                lines += ["        vsync unpack on=pack"]
            lines += [
                f"        for j in 0..{GROUP}",
                f"            vunpk v{{j}} <- l1[{g} + j*8] f16",
                f"        for j in 0..{GROUP}",
                "            vmul v{j}, v{j}, s1",
                # Unpack -> pack: the pack overwrites the words the unpack reads.
                "        vsync pack on=unpack",
                f"        for j in 0..{GROUP}",
                f"            vpack v{{j}} -> l1[{g} + j*8] f16",
            ]
        lines += ["        vsync drain on=pack"]
    lines += [
        "        vdrain a1 <- l1[0] rel",
        "        vsync fill on=drain",
        "    halt",
    ]
    return name, "\n" + "\n".join(lines) + "\n"


@functools.cache
def i79_x(sysnodes: tuple[int, ...]) -> np.ndarray:
    """Every producer's shard of epoch-0 tag words, producer-major."""
    return np.concatenate([shard(s, I79_ROWS * I79_WIDTH) for s in sysnodes])


def i79(sysnodes, pieces, stamped: bool, images: dict) -> tuple[str, str, Spec]:
    """ISSUES 79's fn: the fn's name, text and Spec."""
    rows, width, readers = I79_ROWS, I79_WIDTH, I79_READERS
    producers, consumers = list(sysnodes), list(sysnodes[:readers])
    pitch = len(producers) * width
    b = Body()
    b.barrier(producers, START)
    for q, s in enumerate(producers):
        name, text = i79_image(pieces, pitch, q if stamped else None)
        images[name] = text
        starts = list(itertools.accumulate(pieces, initial=0))[:-1]
        srcs = ", ".join(f"x + {q * rows * width + WORD * a}" for a in starts)
        b.add(s, "send vc[0]", f"    run {name}({rows}, {srcs}, y + {q * width})")
        b.add(s, "wait vc[0]")
        for c in consumers:
            if c != s:
                b.ring(s, c, DONE)
    region, tile = rows // readers * pitch, TILE_WORDS * WORD
    get, get_text = get_image(TILE_WORDS)
    images[get] = get_text
    for r, c in enumerate(consumers):
        b.take(c, DONE, len(producers) - 1)
        run = f"    run {get}({region // tile}, y + {r * region}, v + {r * tile})"
        b.add(c, "send vc[0]", run, "wait vc[0]")
    fn = f"c2_i79_{'_'.join(map(str, pieces))}_{'stamp' if stamped else 'fresh'}"
    params = (
        f"x: f16[{len(producers) * rows * width // 2}] @dram, "
        f"y: f16[{rows * pitch // 2}] @dram, v: f16[{readers * tile // 2}] @dram"
    )

    def inputs(epoch: int) -> dict:
        x = i79_x(tuple(producers)) + np.uint64(check.epoch_bits(epoch))
        return {"x": x.view(np.float16)}

    def want(ins: dict) -> dict:
        x = ins["x"].view(np.uint64)
        x = stamp.stamped(x, len(producers)) if stamped else x
        y = x.reshape(len(producers), rows, width // 8).transpose(1, 0, 2).reshape(-1)
        v = [
            y[((r + 1) * region - tile) // 8 : (r + 1) * region // 8]
            for r in range(readers)
        ]
        return {"y": y.view(np.float16), "v": np.concatenate(v).view(np.float16)}

    geometry = stamp.Gather(tuple(producers), rows, width, readers, tile, stamped)
    spec = Spec(("y", "v"), want, inputs=inputs, provenance=geometry)
    return fn, b.fn(fn, params), spec


def cases(p: int) -> list[Case]:
    base = Case("vc", BURST, TOTAL // (p * BURST), p, "packed")
    out = [base, dataclasses.replace(base, rows=8192)]
    out += [
        dataclasses.replace(base, width=w, rows=TOTAL // (p * w))
        for w in (4 * KB, 16 * KB, 32 * KB, 64 * KB)
    ]
    out += [dataclasses.replace(base, writers=k) for k in (1, 2) if k < p]
    out += [dataclasses.replace(base, place=pl) for pl in ("adjacent", "same")]
    out += [dataclasses.replace(base, rows=64)]
    out += [dataclasses.replace(base, agent=a) for a in ("mg", "mover")]
    return out


def generate(sysnodes: tuple[int, ...], board: str) -> Module:
    text, images, specs = "", {}, {}
    for c in cases(len(sysnodes)):
        src, spec = write(sysnodes, c, images)
        text += src
        specs[c.name] = spec
    for pieces, stamped in itertools.product(PIECES, (False, True)):
        name, src, spec = i79(sysnodes, pieces, stamped, images)
        text += src
        specs[name] = spec
    return Module(HEAD + "".join(images.values()) + text, specs)
