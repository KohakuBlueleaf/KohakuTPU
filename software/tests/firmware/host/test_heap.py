"""The firmware's region heap against a model of its first-fit policy."""

import random

import native
import pytest

from kohakuaccel.driver.node.queue import layout as queue_layout

LAYOUT = queue_layout.Layout.from_machine(
    native.SOFTWARE.parent / "machines" / "multimesh_v9t6.json"
)
GEOMETRIES = [
    (1, 0x40_0000, 1 << 20, 64, 64),
    (2, 1 << 39 | 1 << 36 | 0x10000, 64 << 10, 64, 64),
    (3, 0x1000, 8192, 32, 5),  # a tiny table: NO_SLOTS is reached
    (4, 0x40, 1 << 16, 64, 1024),  # a base off 4 KB: pads in front
]
OPS = 3000
MUTANTS = {
    "no_round": ("(bytes + g - 1) & ~(g - 1)", "bytes"),
    "no_alignment": ("uint64_t a = align > g ? align : g;", "uint64_t a = g;"),
    "no_slot_bound": ("if (h->n + need > h->cap)", "if (0)"),
    "no_merge_after": ("if (i + 1 < h->n && !h->b[i + 1].used)", "if (0)"),
    "no_merge_before": ("if (i > 0 && !h->b[i - 1].used)", "if (0)"),
}


class Model:
    """First fit over [off, len, used, tag] blocks in address order, free
    neighbours merged, addresses aligned to max(align, granule)."""

    def __init__(self, base, nbytes, granule, cap):
        self.base, self.granule, self.cap = base, granule, cap
        self.bytes = nbytes & ~(granule - 1)
        self.blocks = [[0, self.bytes, 0, 0]]

    def alloc(self, size, align, tag):
        if align & (align - 1):
            return LAYOUT.bad_arg, 0
        if size > self.bytes:
            return LAYOUT.no_memory, 0
        g = self.granule
        length = (size + g - 1) & ~(g - 1)
        a = max(align, g)
        for i, (off, n, used, _) in enumerate(self.blocks):
            at = ((self.base + off + a - 1) & ~(a - 1)) - self.base
            pad = at - off
            if used or n < length or pad > n or n - pad < length:
                continue
            rest = n - pad - length
            if len(self.blocks) + bool(pad) + bool(rest) > self.cap:
                return LAYOUT.no_slots, 0
            parts = [[off, pad, 0, 0]] if pad else []
            parts.append([at, length, 1, tag])
            parts += [[at + length, rest, 0, 0]] if rest else []
            self.blocks[i : i + 1] = parts
            return LAYOUT.ok, self.base + at
        return LAYOUT.no_memory, 0

    def free(self, addr):
        off = addr - self.base
        hit = [i for i, b in enumerate(self.blocks) if b[0] == off and b[2]]
        if not hit:
            return LAYOUT.bad_free
        i = hit[0]
        self.blocks[i][2:] = [0, 0]
        if i + 1 < len(self.blocks) and not self.blocks[i + 1][2]:
            self.blocks[i][1] += self.blocks.pop(i + 1)[1]
        if i and not self.blocks[i - 1][2]:
            self.blocks[i - 1][1] += self.blocks.pop(i)[1]
        return LAYOUT.ok


def trace(seed, base, nbytes, granule, cap):
    """The ops, and what each must answer: (rc, value, blocks) or a block list."""
    rng = random.Random(seed)
    ref = Model(base, nbytes, granule, cap)
    lines = [f"i {base} {nbytes} {granule} {cap}"]
    want = [(LAYOUT.ok, 0, 1)]
    live = []
    for k in range(OPS):
        if live and rng.random() < 0.45:
            addr = live.pop(rng.randrange(len(live)))
            if rng.random() < 0.05:
                live.append(addr)
                addr += granule  # inside a block, or past it: BAD_FREE either way
            lines.append(f"f {addr}")
            rc, v = ref.free(addr), 0
        else:
            size = rng.choice(
                (1, granule, 3 * granule + 1, rng.randrange(1, nbytes // 8))
            )
            size = nbytes + granule if rng.random() < 0.02 else size
            align = rng.choice(
                (0, 0, granule, 4 * granule, 4096, 3 if k % 97 == 0 else 0)
            )
            lines.append(f"a {size} {align} {k}")
            rc, v = ref.alloc(size, align, k)
            if rc == LAYOUT.ok:
                live.append(v)
        want.append((rc, v, len(ref.blocks)))
        if k % 64 == 63:
            lines.append("d")
            want.append([tuple(b) for b in ref.blocks])
    return lines, want


def mismatch(out, want):
    """The first answer that is not the model's, as text; None when all are."""
    i = 0
    for n, w in enumerate(want):
        if isinstance(w, list):
            got = []
            while i < len(out) and out[i] != "E":
                _, off, length, used, tag = out[i].split()
                got.append((int(off), int(length), int(used), int(tag)))
                i += 1
            i += 1
            if got != w:
                return f"op {n}: blocks {got[:4]} want {w[:4]}"
        else:
            if i >= len(out):
                return f"op {n}: no answer"
            rc, v, check, blocks = (int(x) for x in out[i].split())
            i += 1
            if (rc, v, blocks) != w or check:
                return f"op {n}: {rc, v, check, blocks} want {w}"
    return None if i == len(out) else "extra answers"


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("heap")
    return out, native.build(out, "heap_trace", "os/heap/heap.c")


@pytest.mark.parametrize("seed,base,nbytes,granule,cap", GEOMETRIES)
def test_every_answer_is_the_models(built, seed, base, nbytes, granule, cap):
    lines, want = trace(seed, base, nbytes, granule, cap)
    assert mismatch(native.run(built[1], lines), want) is None
    statuses = {w[0] for w in want if isinstance(w, tuple)}
    assert {LAYOUT.ok, LAYOUT.bad_free, LAYOUT.no_memory} <= statuses
    assert cap != 5 or LAYOUT.no_slots in statuses


def test_bad_geometry_is_refused(built):
    cases = [(0x40, 4096, 48, 8), (0x20, 4096, 64, 8), (0, 32, 64, 8), (0, 4096, 64, 2)]
    out = native.run(built[1], [f"i {b} {n} {g} {c}" for b, n, g, c in cases])
    assert [int(line.split()[0]) for line in out] == [LAYOUT.bad_arg] * len(cases)


@pytest.mark.parametrize("name", MUTANTS)
def test_a_mutant_is_told(built, name):
    exe = native.build(built[0], "heap_trace", "os/heap/heap.c", MUTANTS[name])
    told = []
    for geometry in GEOMETRIES:
        lines, want = trace(*geometry)
        try:
            told.append(mismatch(native.run(exe, lines), want))
        except native.subprocess.CalledProcessError as e:
            told.append(str(e))
    assert any(told)
