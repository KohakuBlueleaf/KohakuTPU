"""Element-granular relayouts on the card: f16 elements walked into any
other order, exact, through no form wider than the data.

A destination word's lanes group into taps: the lanes reading one source
word at one rotation r (lane l reads lane ``(l + r) mod 16``). Words with the
same taps form a class: the mover gathers each tap's source words (fp32
index lists), a vector core makes ``where(m_t, xrot(S_t, r_t), ..., base)``
(`base` 0 or the destination's word, GROUP taps a pass), the mover scatters
the words back. Rows moved between pitches take two taps a word, a strided
read up to 16. Words no element reaches are 0 (one fill), or kept.
"""

from collections import OrderedDict
from dataclasses import dataclass

import numpy as np

from kohakutpu.application.kernels import vector as V
from kohakutpu.language import dsl as kt

LANES = 16
#: 128-value rows a vector trip takes (8 words each): GROUP taps, the
#: result and their second buffers fit a core's 512-word L1.
ROWS = 2
#: Taps one vector pass reads: a tap and its mask are a memory view each,
#: with the result's load and store within a core's 8 descriptors.
GROUP = 3
#: Relayout plans and generated kernels kept in memory.
PLANS_KEPT = 1024
KERNELS_KEPT = 1024
_plans: OrderedDict = OrderedDict()
_kernels: OrderedDict = OrderedDict()


@dataclass(frozen=True)
class Class:
    """Destination words that read the same taps."""

    taps: tuple  # ((rotation, lane mask bits), ...), first lane order
    words: int
    keep: bool  # lanes outside every tap keep the destination's value


@dataclass(frozen=True)
class Plan:
    """A relayout: its classes, their fp32 index lists (per class, per tap
    then the destination, per column block, `rows(class)` entries each) and
    the taps' lane masks ([taps, 128] f16)."""

    classes: tuple
    index: np.ndarray
    masks: np.ndarray
    src_words: int
    dst_words: int
    fill: bool


def rows(c: Class) -> int:
    """128-value rows of a class's scratch: whole trips of ROWS."""
    return -(-c.words // (LANES // 2 * ROWS)) * ROWS


def entries(c: Class) -> int:
    """Indices one of a class's lists holds: its rows, in whole 32-byte
    words (the mover reads an index list from a word boundary)."""
    return -(-rows(c) // 8) * 8


def elements(walk, at: int) -> np.ndarray:
    """The elements ``at + sum(i_k * stride_k)`` of `walk` (``(count,
    element stride)``, outermost first), in walk order."""
    idx = np.array([at], np.int64)
    for n, s in walk:
        idx = (idx[:, None] + np.arange(n, dtype=np.int64)[None, :] * s).reshape(-1)
    return idx


def plan(src_n: int, walk, at: int, dwalk, dat: int, m: int, keep: bool) -> Plan:
    """The relayout of `m` destination elements from element `at` of `src_n`
    source elements walked by `walk` into the elements `dwalk` reaches from
    `dat` (the two walks' counts equal)."""
    w_key = tuple((int(n), int(s)) for n, s in walk)
    dw_key = tuple((int(n), int(s)) for n, s in dwalk)
    pkey = (int(src_n), w_key, int(at), dw_key, int(dat), int(m), bool(keep))
    hit = _plans.get(pkey)
    if hit is not None:
        _plans.move_to_end(pkey)
        return hit
    src, dst = elements(w_key, at), elements(dw_key, dat)
    if len(src) != len(dst) or (len(src) and (src.min() < 0 or src.max() >= src_n)):
        raise ValueError(f"walks of {len(src)} / {len(dst)} elements over {src_n}")
    if len(dst) and (dst.min() < 0 or dst.max() >= m):
        raise ValueError(f"a destination element outside 0..{m}")
    dw = -(-m // LANES)
    lanes = np.full(dw * LANES, -1, np.int64)
    lanes[dst] = src
    grid = lanes.reshape(dw, LANES)
    covered = grid >= 0
    lane = np.arange(LANES)
    word = np.where(covered, grid // LANES, -1)
    rot = np.where(covered, (grid % LANES - lane) % LANES, -1)
    active_w = np.flatnonzero(covered.any(1))
    word_rows = word[active_w].tolist()
    rot_rows = rot[active_w].tolist()
    whole_rows = covered[active_w].all(1).tolist()
    bits_table = [1 << l for l in range(LANES)]
    groups: dict = {}
    for w, w_row, r_row, whole in zip(
        active_w.tolist(), word_rows, rot_rows, whole_rows
    ):
        taps: dict = {}
        for q, r, b in zip(w_row, r_row, bits_table):
            if q >= 0:
                key = (q, r)
                taps[key] = taps.get(key, 0) | b
        sig = tuple((r, mask) for (_, r), mask in taps.items())
        groups.setdefault((sig, keep and not whole), []).append(
            (*(q for (q, _) in taps), w)
        )
    classes, lists, masks = [], [], []
    for (sig, kept), members in groups.items():
        c = Class(sig, len(members), kept)
        ix = np.zeros((entries(c), LANES // 2, len(sig) + 1), np.float32)
        ix.reshape(-1, len(sig) + 1)[: len(members)] = members
        classes.append(c)
        lists.append(ix.transpose(2, 1, 0).reshape(-1))
        for _, mask in sig:
            bits = (mask >> lane) & 1
            masks.append(np.tile(bits, 128 // LANES).astype(np.float16))
    fill = not keep and not covered.all()
    got = Plan(
        tuple(classes),
        np.concatenate(lists) if lists else np.zeros(8, np.float32),
        np.stack(masks) if masks else np.zeros((1, 128), np.float16),
        -(-src_n // LANES),
        dw,
        fill,
    )
    if PLANS_KEPT > 0:
        _plans[pkey] = got
        while len(_plans) > PLANS_KEPT:
            _plans.popitem(last=False)
    return got


def relayout_kernel(pl: Plan, cores: int):
    """Ports ``src`` (f16 as words), ``dst`` (f16 as words), ``index``
    (`pl.index`, fp32), ``mask`` (`pl.masks`). Returns ``(kernel, given)``."""
    classes = pl.classes
    src_words, dst_words = pl.src_words, pl.dst_words
    index_len = len(pl.index)
    mask_shape = tuple(pl.masks.shape)
    fill = pl.fill
    kkey = (
        classes,
        src_words,
        dst_words,
        index_len,
        mask_shape,
        fill,
        cores,
        V.split,
        kt.Kernel.build,
    )
    hit = _kernels.get(kkey)
    if hit is not None:
        _kernels.move_to_end(kkey)
        return hit
    ports = {
        "src": kt.In(src_words, LANES),
        "dst": kt.Out(dst_words, LANES),
        "index": kt.In(index_len, dtype="f32"),
        "mask": kt.In(*mask_shape),
    }
    given = {
        "src": kt.TensorSpec.make((src_words, LANES)),
        "dst": kt.TensorSpec.make((dst_words, LANES)),
        "index": kt.TensorSpec.make((index_len,), "f32"),
        "mask": kt.TensorSpec.make(mask_shape),
    }

    def body(src, dst, index, mask):
        if fill:
            kt.fill_value(dst, 0)
        # One scratch for every class in turn: its taps' rows, then its result's.
        most = max((len(c.taps) + 1) * rows(c) for c in classes)
        scratch = kt.buffer((most, 128), "f16")
        at, m0 = 0, 0
        for c in classes:
            _class(c, src, dst, index, at, mask, m0, scratch, cores)
            at += (len(c.taps) + 1) * (LANES // 2) * entries(c)
            m0 += len(c.taps)

    shape = tuple((c.taps, c.words, c.keep) for c in classes)
    key = (shape, src_words, dst_words, fill)
    got = (kt.Kernel.build(V.name("lanes", *key), body, ports), given)
    if KERNELS_KEPT > 0:
        _kernels[kkey] = got
        while len(_kernels) > KERNELS_KEPT:
            _kernels.popitem(last=False)
    return got


def _class(
    c: Class, src, dst, index, at: int, mask, m0: int, scratch, cores: int
) -> None:
    """One class: gather its taps' words, assemble, scatter."""
    nr, ne, half = rows(c), entries(c), LANES // 2
    taps = [_Rows(scratch, t * nr) for t in range(len(c.taps))]
    out = _Rows(scratch, len(c.taps) * nr)

    def lists(t: int, j: int):
        lo = at + (t * half + j) * ne
        return index[lo : lo + ne]

    blocks = [(j, -(-(c.words - j) // half)) for j in range(half) if j < c.words]
    for j, n in blocks:
        cols = slice(LANES * j, LANES * (j + 1))
        for t, s in enumerate(taps):
            kt.gather(s[0:n, cols], src, lists(t, j), fp=True)
        if c.keep:
            kt.gather(out[0:n, cols], dst, lists(len(c.taps), j), fp=True)
    for g in range(0, len(c.taps), GROUP):
        _assemble(c, taps, out, mask, m0, g, cores)
    for j, n in blocks:
        cols = slice(LANES * j, LANES * (j + 1))
        kt.scatter(dst, out[0:n, cols], lists(len(c.taps), j), fp=True)


class _Rows:
    """Rows of the scratch from `first` on, indexed from 0."""

    def __init__(self, buf, first: int) -> None:
        self.buf, self.first = buf, first

    def __getitem__(self, key):
        rows, cols = key
        lo, hi = self.first + rows.start, self.first + rows.stop
        return self.buf[lo:hi, cols]


def _assemble(c: Class, taps, out, mask, m0: int, g: int, cores: int) -> None:
    """Taps g..g+GROUP of every word of the class, over `out`: from what it
    holds (a kept destination, or an earlier group's taps), else from 0."""
    group = range(g, min(g + GROUP, len(c.taps)))
    trips = rows(c) // ROWS
    ncore, start, n = V.split(trips, cores)
    for core in kt.par(ncore, on=kt.vc):
        ms = {t: kt.unpack(kt.load(mask[m0 + t : m0 + t + 1, :])) for t in group}
        for k in kt.range(n(core)):
            r = (start(core) + k) * ROWS
            base = kt.unpack(kt.load(out[r : r + ROWS, :])) if c.keep or g else 0.0
            for t in group:
                rot, bits = c.taps[t]
                v = kt.unpack(kt.load(taps[t][r : r + ROWS, :]))
                v = kt.xrot(v, rot) if rot else v
                base = v if bits == (1 << LANES) - 1 else kt.where(ms[t], v, base)
            kt.store(out[r : r + ROWS, :], base)


__all__ = [
    "GROUP",
    "Class",
    "Plan",
    "elements",
    "entries",
    "plan",
    "relayout_kernel",
    "rows",
]
