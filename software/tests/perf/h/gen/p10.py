"""P10: read rates through the Xache, by vector core (fills into L1) and by cluster
(FILL into A), two reads in flight a trip. A read is spread (a footprint 4 KiB to
64 MiB, contiguous over every interleave stripe) or striped (16 KiB stripe k of
every 64 KiB, k = 0..3 from the buffer's base: one home, which home being the bound
address's). One reader on each sysnode, then two and four sysnodes at once after a
start barrier, on disjoint spans or on the same span; a spread read once (cold) and
again (warm, a second run). A core writes the last trip it read to y; x is host
tag words, written once.

Not here: a cluster's read leaves no data to check (no output); which Xache home a
stripe is on follows from the base the runner binds, which this row does not pick."""

import dataclasses
import itertools

import numpy as np

from h.gen import Module, Spec
from h.gen.body import HEAD, Body, host_tags

KB, MB = 1 << 10, 1 << 20
SPREAD = (4 * KB, 64 * KB, MB, 16 * MB, 64 * MB)
#: Striped reads' bytes read (the span is four times that).
STRIPED = (32 * KB, MB, 16 * MB)
STRIPE, PERIOD, STRIPES = 16 * KB, 64 * KB, 4
#: The many-reader cases: spread footprints, and the stripes' bytes read.
MANY_SPREAD = (MB, 64 * MB)
MANY_STRIPED = MB
#: A read's bytes at most: a core's half L1 (256 words), a cluster's 128 entries.
MOST = {"vc": 256 * 32, "mg": 128 * 128}
ENTRY = 128
START = 1


@dataclasses.dataclass(frozen=True)
class Read:
    """`size` bytes read spread, or from stripe `k` (k None: spread), by `engine`."""

    engine: str
    size: int
    k: int | None

    @property
    def each(self) -> int:
        return min(self.size // 2, MOST[self.engine], STRIPE)

    @property
    def trips(self) -> int:
        return self.size // (2 * self.each)

    def at(self, i: int) -> int:
        """Read i's offset from the base."""
        b = i * self.each
        return b if self.k is None else b % STRIPE + b // STRIPE * PERIOD

    @property
    def base(self) -> int:
        return 0 if self.k is None else self.k * STRIPE

    @property
    def span(self) -> int:
        return self.size if self.k is None else self.size // STRIPE * PERIOD

    @property
    def image(self) -> str:
        return f"rd_{self.each}_{self.at(1)}_{self.at(2)}"

    def text(self) -> str:
        """The core's image: two fills a trip, the last trip written to y."""
        w, half = self.each // 32, MOST["vc"] // 32
        return f"""
image {self.image}(n, x, y)
    desc a0 = x walk=(32, {w})
    desc a1 = x + {self.at(1)} walk=(32, {w})
    desc a2 = y walk=(32, {w})
    desc a3 = y + {self.each} walk=(32, {w})
    ainc a0 += {self.at(2)}
    ainc a1 += {self.at(2)}
    seti s0 = n
    loop s0
        vfill a0 -> l1[0] rel
        vfill a1 -> l1[{half}] rel
        vsync fe on=fill
    vdrain a2 <- l1[0]
    vdrain a3 <- l1[{half}]
    vsync fe on=drain
    halt
"""


def run(b: Body, r: Read, n: int, x: int, q: int, p: int) -> None:
    """Sysnode `n`'s pass `p` over its span at x + `x` (reader `q`)."""
    if r.engine == "vc":
        args = f"{r.trips}, x + {x + r.base}, y + {2 * q * r.each}"
        b.add(n, "send vc[0]", f"    run {r.image}({args})", "wait vc[0]")
        return
    fill = f"n={r.each // ENTRY}"
    b.add(
        n,
        "r3 = set 0",
        f"r4 = set {r.trips}",
        f"label trip{p}",
        f"r5 = mul r3, {r.at(2)}",
        "send mg[0]",
        f"    fill A0 <- x + {x + r.base} + r5 {fill}",
        f"    fill A1 <- x + {x + r.base + r.at(1)} + r5 {fill}",
        "r3 = add r3, 1",
        f"goto trip{p} if r3 < r4",
        "wait mg[0]",
    )


def reads(r: Read, readers: list[int], disjoint: bool, passes: int):
    b = Body()
    if len(readers) > 1:
        b.barrier(readers, START)
    starts = [q * r.span if disjoint else 0 for q in range(len(readers))]
    for (q, n), p in itertools.product(enumerate(readers), range(passes)):
        run(b, r, n, starts[q], q, p)
    where = f"stripe{r.k}_{r.size >> 10}k" if r.k is not None else f"{r.size >> 10}k"
    many = ("_disjoint" if disjoint else "_same") if len(readers) > 1 else ""
    warm = "_warm" if passes > 1 else ""
    name = f"p10_{r.engine}_{''.join(map(str, readers))}{many}_{where}{warm}"
    params = f"x: f16[{(starts[-1] + r.span) // 2}] @dram"
    if r.engine == "mg":
        return name, b.fn(name, params), Spec(())
    params += f", y: f16[{len(readers) * r.each}] @dram"
    last = (2 * r.trips - 2, 2 * r.trips - 1)

    def want(ins: dict) -> dict:
        x = ins["x"]
        at = [s + r.base + r.at(i) for s in starts for i in last]
        return {"y": np.concatenate([x[a // 2 : (a + r.each) // 2] for a in at])}

    tagged = host_tags({"x": (starts[-1] + r.span, np.float16, 0)})
    return name, b.fn(name, params), Spec(("y",), want, inputs=tagged, fresh=False)


def generate(sysnodes: tuple[int, ...], board: str) -> Module:
    cases = []
    for engine in MOST:
        spread = [Read(engine, size, None) for size in SPREAD]
        striped = [Read(engine, size, k) for size in STRIPED for k in range(STRIPES)]
        for n in sysnodes:
            cases += [(r, [n], False, p) for r in spread for p in (1, 2)]
            cases += [(r, [n], False, 1) for r in striped]
        many = [Read(engine, size, None) for size in MANY_SPREAD]
        many += [Read(engine, MANY_STRIPED, k) for k in range(STRIPES)]
        groups = (
            [list(sysnodes[:2]), list(sysnodes)]
            if len(sysnodes) > 2
            else [list(sysnodes)]
        )
        for r, group, disjoint in itertools.product(many, groups, (True, False)):
            cases.append((r, group, disjoint, 1))
    images = {r.image: r.text() for r, *_ in cases if r.engine == "vc"}
    text, specs = HEAD + "".join(images.values()), {}
    for case in cases:
        name, src, spec = reads(*case)
        text += src
        specs[name] = spec
    return Module(text, specs)
