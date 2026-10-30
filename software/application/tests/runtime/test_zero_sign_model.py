"""zero_sign on the card model (L2.VC.026, ISA.VC.058): each kernel moves,
negates, selects or takes a max / min of its input, compiled under both
values. `preserve` returns the sign of a zero as the operation defines it,
bit for bit: y equals the reference below for every input, both zeros,
subnormals, +-inf and NaN included (a NaN compares as any NaN). `native`
returns the reference with each -0 (0x8000) as +0. Each preserve mutant
brings the native form back on its kernel.

Max and min on a tie return the first operand (max(-0, +0) is -0), as vmax
does for every other tie. The crossbar kernels permute lanes, so their
witness is the multiset of a trip's outputs.
"""

import json

import numpy as np
import pytest

from kohakutpu.language.text.syntax import TextError
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs

NEG0, POS0 = 0x8000, 0x0000
HEAD = """target ktpu.v9

fn k.l2(x: f16[8, 128] @dram, y: f16[8, 128] @dram)
    par c in 0..1 on vc[c]
        for t in 0..1
            xl = load x[t*{r} : +{r}, :]   : f16[{r}, 128] @l1
            v = unpack xl                      : f32[{r}, 128] @reg
"""
TAIL = """            ol = pack o                     : f16[{r}, 128] @l1
            store y[t*{r} : +{r}, :] <- ol
"""
ROW = ": f32[{r}, 128] @reg"
#: name -> the lines between the unpack of v and the pack of o.
BODIES = {
    "mov": [f"o = mov v {ROW}"],
    "neg": [f"o = neg v {ROW}"],
    "max0": [f"o = max v, 0.0 {ROW}"],
    "min0": [f"o = min v, 0.0 {ROW}"],
    "maxvv": [f"o = max v, v {ROW}"],
    "maxneg0": [f"o = max v, -0.0 {ROW}"],
    "sel_fused": [f"m = cmplt v, 0.0 {ROW}", f"o = sel m, v, v {ROW}"],
    "sel_unfused": [
        f"m = cmplt v, 0.0 {ROW}",
        f"p = sel m, v, v {ROW}",
        f"o = sel m, p, v {ROW}",
    ],
    "xlane": [f"o = xlane v, 1 {ROW}"],
    "xrot": [f"o = xrot v, 1 {ROW}"],
    "rowmax": ["m = rmax v : f32[{r}] @reg", f"o = max v, m[:, *] {ROW}"],
}
PERMUTED = {"xlane", "xrot"}
#: Rows a trip moves: a row reduction lowers in groups of 8 rows.
ROWS = {"rowmax": 8}


def rows_of(name):
    return ROWS.get(name, 4)


def text(name):
    body = HEAD + "".join(f"            {s}\n" for s in BODIES[name]) + TAIL
    return body.format(r=rows_of(name))


def edges():
    """Both zeros and both signs of 1, -3, the smallest and largest
    subnormal, the largest normal and inf, then NaN."""
    bits = [POS0, NEG0, 0x3C00, 0xBC00, 0xC200, 0x0001, 0x8001, 0x03FF, 0x83FF]
    return bits + [0x7BFF, 0xFBFF, 0x7C00, 0xFC00, 0x7E00]


def values():
    """One trip: the edges, then seeded f16 of both signs over the whole
    range (subnormals included), with a zero of each sign every 16 lanes."""
    rng = np.random.default_rng(26)
    rest = rng.integers(0x0001, 0x7C00, 4 * 128 - len(edges()), dtype=np.uint16)
    rest |= rng.integers(0, 2, rest.size, dtype=np.uint16) << 15
    bits = np.concatenate([np.array(edges(), np.uint16), rest])
    bits[32::16] = np.where(np.arange(bits[32::16].size) % 2, NEG0, POS0)
    return bits


def extremes(n, cols, seed):
    """`n` rows of `cols`, no NaN, a cycle of four rows (seeded apart): a row
    of negatives and one -0 (max -0), a row of negatives (max the least
    negative), a mixed row, and a row of negatives and one +0."""
    rng = np.random.default_rng(seed)
    out = rng.integers(0x8001, 0xFC00, (n, cols), dtype=np.uint16)
    for i in range(n):
        if i % 4 == 0:
            out[i, rng.integers(cols)] = NEG0
        if i % 4 == 2:
            out[i] = rng.integers(0x0001, 0x7C00, cols, dtype=np.uint16)
            out[i] |= rng.integers(0, 2, cols, dtype=np.uint16) << 15
        if i % 4 == 3:
            out[i, rng.integers(cols)] = POS0
    return out


def rows():
    """One trip for the row max: `extremes` of 8 rows."""
    return extremes(8, 128, 27).ravel()


def extreme(bits):
    """The bits of a row's max: those of every element at it, or 0xFFFF
    where they differ (both zeros)."""
    v = f16(bits)
    top = v.max()
    at = {int(b) for b, x in zip(bits, v) if x == top}
    return at.pop() if len(at) == 1 else 0xFFFF


def f16(bits):
    return np.asarray(bits, np.uint16).view(np.float16)


def canonical(bits):
    """Bits with every NaN as 0x7E00."""
    bits = np.array(bits, np.uint16)
    bits[np.isnan(f16(bits))] = 0x7E00
    return bits


def reference(name, x):
    """The bits `preserve` returns for input bits `x`."""
    v = f16(x)
    if name in ("mov", "maxvv", "sel_fused", "sel_unfused"):
        return x.copy()
    if name == "neg":
        return np.where(np.isnan(v), x, x ^ NEG0).astype(np.uint16)
    if name == "max0":
        return np.where(v < 0, POS0, x).astype(np.uint16)
    if name == "min0":
        return np.where(v > 0, POS0, x).astype(np.uint16)
    if name == "maxneg0":
        return np.where(v < 0, NEG0, x).astype(np.uint16)
    if name == "rowmax":
        out = x.copy().reshape(-1, 128)
        for r in out:
            r[:] = extreme(r)
        return out.ravel()
    raise KeyError(name)


def flush(bits):
    return np.where(bits == NEG0, POS0, bits).astype(np.uint16)


def run(dev, name, zero_sign, x, mutant=None):
    with rs.zero_sign(zero_sign), rs.mutant(mutant):
        c = K.compile(text(name), name="k", machine=dev.machine)
    data = np.zeros((8, 128), np.uint16)
    data.ravel()[: x.size] = x
    xa, ya = dev.alloc(data.nbytes), dev.alloc(data.nbytes)
    try:
        dev.write(xa, data.tobytes())
        dev.write(ya, bytes(data.nbytes))
        dev.run(c, {"x": xa, "y": ya})
        y = np.frombuffer(dev.read(ya, data.nbytes), np.uint16)
    finally:
        dev.free(xa)
        dev.free(ya)
    return y[: x.size].copy()


def inputs(name):
    if name in ROWS:
        return rows()
    return values()


def want(name, zero_sign, x):
    ref = reference(name, x) if name not in PERMUTED else np.sort(x)
    ref = ref if zero_sign == "preserve" else flush(ref)
    return np.sort(canonical(ref)) if name in PERMUTED else canonical(ref)


def got(name, y):
    return np.sort(canonical(y)) if name in PERMUTED else canonical(y)


def report(name, zero_sign, x, y, w):
    bad = [
        f"{int(a):04x}->{int(b):04x} (want {int(c):04x})"
        for a, b, c in zip(x, got(name, y), w)
        if b != c
    ]
    print(json.dumps({"kernel": name, "zero_sign": zero_sign, "bad": bad[:64]}))
    return bad


def test_inputs_hold_both_zeros_inf_nan_and_subnormals():
    x = values()
    for b in (POS0, NEG0, 0x7C00, 0xFC00, 0x0001, 0x8001):
        assert b in x
    assert np.isnan(f16(x)).any()
    assert rows().size == rows_of("rowmax") * 128
    for r in (0, 4):
        assert reference("rowmax", rows())[r * 128] == NEG0
        assert reference("rowmax", rows())[(r + 3) * 128] == POS0
    x = stat_inputs()
    assert x.shape == (16, 32)
    assert {extreme(r) for r in x} >= {NEG0, POS0}
    assert tiled(np.arange(16 * 32).reshape(16, 32))[:20].tolist() == [
        *range(4),
        *range(32, 36),
        *range(64, 68),
        *range(96, 100),
        *range(4, 8),
    ]


@pytest.mark.parametrize("zero_sign", ["native", "preserve"])
@pytest.mark.parametrize("name", list(BODIES))
def test_kernel_returns_the_reference(dev, name, zero_sign):
    x = inputs(name)
    w = want(name, zero_sign, x)
    assert not (w == 0xFFFF).any(), "a row max with mixed-sign zeros"
    y = run(dev, name, zero_sign, x)
    assert not report(name, zero_sign, x, y, w)


#: A GT4 row max packed 16 rows a word (L2.STAT.003): its band trees, then
#: the compact into one word.
STAT = """target ktpu.v9

fn k.l2(x: f16[16, 32] @dram(tile=1x8), s: f16[16] @dram)
    par c in 0..1 on vc[c]
        for t in 0..1
            xl = load x                              : f16[16, 32] @l1(gt4)
            v = unpack xl                            : f32[16, 32] @reg(gt4)
            m = rmax v                               : f32[16] @reg
            p = pack m                               : f16[16] @l1
            store s <- p
"""


def stat_inputs():
    return extremes(16, 32, 29)


def tiled(x):
    """Row-major `x` as `@dram(tile=1x8)`: 4-row bands of 4 x 4 sub-tiles,
    sub-tiles and their elements row-major (L2.SPACE.002)."""
    n, cols = x.shape
    return x.reshape(n // 4, 4, cols // 4, 4).transpose(0, 2, 1, 3).ravel()


def stat_want(zero_sign, x):
    bits = np.array([extreme(r) for r in x], np.uint16)
    return flush(bits) if zero_sign == "native" else bits


def run_stat(dev, zero_sign, x, mutant=None):
    with rs.zero_sign(zero_sign), rs.mutant(mutant):
        c = K.compile(STAT, name="k", machine=dev.machine)
    data = tiled(x)
    xa, sa = dev.alloc(data.nbytes), dev.alloc(32)
    try:
        dev.write(xa, data.tobytes())
        dev.write(sa, bytes(32))
        dev.run(c, {"x": xa, "s": sa})
        out = dev.read(sa, 32)
    finally:
        dev.free(xa)
        dev.free(sa)
    return np.frombuffer(out, np.uint16).copy()


@pytest.mark.parametrize("zero_sign", ["native", "preserve"])
def test_statistic_returns_the_reference(dev, zero_sign):
    x = stat_inputs()
    w = stat_want(zero_sign, x)
    y = run_stat(dev, zero_sign, x)
    bad = [
        f"row {r}: {a:04x} (want {b:04x})"
        for r, (a, b) in enumerate(zip(y, w))
        if a != b
    ]
    print(json.dumps({"statistic": "rmax", "zero_sign": zero_sign, "bad": bad}))
    assert not bad


#: mutant -> the kernel it breaks under preserve.
MUTANTS = {
    "l2.zero_sign/native-copy": "mov",
    "l2.zero_sign/native-minmax": "max0",
    "l2.zero_sign/native-sel": "sel_unfused",
    "l2.zero_sign/fold-negzero": "maxneg0",
}


@pytest.mark.parametrize("mutant", list(MUTANTS))
def test_each_mutant_flushes_its_kernel(dev, mutant):
    name = MUTANTS[mutant]
    x = inputs(name)
    y = got(name, run(dev, name, "preserve", x, mutant))
    w = want(name, "preserve", x)
    differ = {(int(a), int(b)) for a, b in zip(w, y) if a != b}
    assert differ == {(NEG0, POS0)}, differ


def test_a_compare_off_its_destination_chunks_breaks_the_row_max(dev):
    """A pick's compare without its destination's chunk select leaves the
    predicated copy reading P1 bits no compare wrote (ISA.VC.080, .081)."""
    x = inputs("rowmax")
    y = got(
        "rowmax", run(dev, "rowmax", "preserve", x, "l2.zero_sign/compare-unviewed")
    )
    w = want("rowmax", "preserve", x)
    assert {(int(a), int(b)) for a, b in zip(w, y) if a != b} - {(NEG0, POS0)}


@pytest.mark.parametrize("kernel", ["rowmax", "statistic"])
def test_a_merge_tree_under_preserve_breaks_its_kernel(dev, kernel):
    """A merge also redirects the a operand (ISA.VC.076), so no copy of a
    merge operand is exact: with the row tree's and the compact's merges kept
    under preserve, the results are wrong beyond a flushed zero."""
    mutant = "l2.vector/merge-tree-picked"
    if kernel == "rowmax":
        x = inputs("rowmax")
        y = got("rowmax", run(dev, "rowmax", "preserve", x, mutant))
        w = want("rowmax", "preserve", x)
    else:
        x = stat_inputs()
        y = run_stat(dev, "preserve", x, mutant)
        w = stat_want("preserve", x)
    assert {(int(a), int(b)) for a, b in zip(w, y) if a != b} - {(NEG0, POS0)}


def special_rows(n):
    return [r for r in range(n) if r // 4 % 2]


def nonfinite_rows(n, cols):
    """`n` rows of `cols` whose finite elements are unique to their row, the
    rows of every second 4-row band also with +inf, -inf and NaN. Row r's
    result is read from lane r of its chunk, and vmax keeps a NaN on the a
    side and drops one on the b side (ISA.VC.054), so the NaN at lane r ^ 4
    stops the +inf at lane r ^ 12 on its way (xor 8, then xor 4) to lane r."""
    col = np.arange(cols)
    x = np.array([np.where(col % 2, -1, 1) * (64.0 * (r + 1) + col) for r in range(n)])
    x = x.astype(np.float16).view(np.uint16)
    for r in special_rows(n):
        lane = r % 16
        x[r, lane ^ 12], x[r, lane ^ 8], x[r, lane ^ 4] = 0x7C00, 0xFC00, 0x7E00
    return x


def foreign_rows(x, y):
    """The rows with a result that is no element of the row (any NaN stands
    for the row's NaN)."""
    return [
        r
        for r, (row, b) in enumerate(zip(x, canonical(y)))
        if set(np.atleast_1d(b).tolist()) - set(canonical(row).tolist())
    ]


@pytest.mark.parametrize("zero_sign", ["native", "preserve"])
def test_a_row_with_nan_reduces_to_an_element_of_the_row(dev, zero_sign):
    x = nonfinite_rows(16, 32)
    y = run_stat(dev, zero_sign, x)
    print(json.dumps({"zero_sign": zero_sign, "s": [f"{int(b):04x}" for b in y]}))
    assert not foreign_rows(x, y)


def test_preserve_drops_the_inf_a_nan_stops(dev):
    """The rows reach the stopped path: a finite result in a row with +inf, so
    the check above sees folds that lost the row's largest element."""
    x = nonfinite_rows(16, 32)
    y = f16(run_stat(dev, "preserve", x))
    assert np.isfinite(y[special_rows(16)]).any(), y


def test_a_compact_off_its_rows_returns_another_rows_element(dev):
    x = nonfinite_rows(16, 32)
    assert foreign_rows(x, run_stat(dev, "preserve", x, "l2.vector/compact-reversed"))


@pytest.mark.parametrize("zero_sign", ["native", "preserve"])
def test_a_row_tree_with_nan_reduces_within_the_row(dev, zero_sign):
    """`rowmax` gives each element max(element, row max): m itself where the
    element is below it, so every output of the row is an element of it."""
    x = nonfinite_rows(8, 128)
    y = run(dev, "rowmax", zero_sign, x.ravel()).reshape(8, 128)
    assert not foreign_rows(x, y)


def test_a_merge_tree_under_preserve_leaves_its_rows(dev):
    x = nonfinite_rows(8, 128)
    mutant = "l2.vector/merge-tree-picked"
    y = run(dev, "rowmax", "preserve", x.ravel(), mutant).reshape(8, 128)
    assert foreign_rows(x, y)


QUANTISED = """target ktpu.v9

fn k.l2(x: f16[16, 128] @dram(tile=16x32), y: mx7a[16, 128] @dram(gm=16, nk=2))
    par c in 0..1 on vc[c]
        for t in 0..1
            xl = load x[0 : +16, 0 : +128]  : f16[16, 128] @l1(gt4)
            v = unpack xl                   : f32[16, 128] @reg(gt4)
            q = pack v                      : mx7a[16, 128] @l1
            store y[0 : +16, 0 : +128] <- q
"""
#: The attention kernel's stream: its 128-column exp2 and row sum with 128
#: zero lifts on the quantised store.
EXPONENT = """target ktpu.v9

fn k.l2(h1: f16[128, 512] @dram(tile=16x32), h2: mx7a[128, 512] @dram(gm=16, nk=2),
        h4: f16[128] @dram, h5: f16[4, 128] @dram)
    par c in 0..1 on vc[c]
        for t in 0..1
            xl9 = load h4[c*16 + t*16 : +16]               : f16[16] @l1
            xl10 = load h1[c*16 + t*16 : +16, 0 : +128]    : f16[16, 128] @l1(gt4)
            v9 = unpack xl10                               : f32[16, 128] @reg(gt4)
            e4 = mul v9, 0.18033688011112042               : f32[16, 128] @reg(gt4)
            v10 = unpack xl9                               : f32[16] @reg
            e5 = mul v10, 0.18033688011112042              : f32[16] @reg
            e6 = sub e4, e5[:, *]                          : f32[16, 128] @reg(gt4)
            e7 = exp2 e6                                   : f32[16, 128] @reg(gt4)
            q1 = pack e7                                   : mx7a[16, 128] @l1
            store h2[c*16 + t*16 : +16, 0 : +128] <- q1
            s5 = rsum e7                                   : f32[16] @reg
            p6 = pack s5                                   : f16[16] @l1
            store h5[0, c*16 + t*16 : +16] <- p6
"""


def quantised(dev, bits):
    """The bytes the store leaves for input bytes `bits` under preserve (the
    quantised block is smaller than the input)."""
    with rs.zero_sign("preserve"):
        c = K.compile(QUANTISED, name="k", machine=dev.machine)
    xa, ya = dev.alloc(bits.nbytes), dev.alloc(bits.nbytes)
    try:
        dev.write(xa, bits.tobytes())
        dev.write(ya, bytes(bits.nbytes))
        dev.run(c, {"x": xa, "y": ya})
        return dev.read(ya, bits.nbytes)
    finally:
        dev.free(xa)
        dev.free(ya)


def test_a_zero_of_either_sign_lifts_on_a_quantised_store(dev):
    """The lift `vsel d, x, k, x` stays the core's select: a zero takes the
    constant whatever its sign, and every other value moves exactly."""
    rng = np.random.default_rng(28)
    x = rng.integers(0x0400, 0x7C00, (16, 128), dtype=np.uint16)
    x |= rng.integers(0, 2, x.shape, dtype=np.uint16) << 15
    pos, neg = x.copy(), x.copy()
    pos[::3, ::5], neg[::3, ::5] = POS0, NEG0
    assert quantised(dev, pos) == quantised(dev, neg)
    assert quantised(dev, pos) != quantised(dev, x)


def test_the_zero_lift_keeps_a_quantised_stream_in_one_image(dev):
    """Picked as a compare and two copies, the 128 lifts take the stream's
    loop past VLOOP's body field."""
    with rs.zero_sign("preserve"):
        c = K.compile(EXPONENT, name="k", machine=dev.machine)
    assert c.l1.source.count("vsel ") == 128
    with (
        rs.zero_sign("preserve"),
        rs.mutant("l2.zero_sign/lift-picked"),
        pytest.raises(TextError, match="no image of the stream loop encodes"),
    ):
        K.compile(EXPONENT, name="k", machine=dev.machine)
