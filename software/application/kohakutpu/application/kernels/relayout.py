"""Relayouts of f16 elements on the card, exact, as one kernel of the
cheapest parts the card has (`forms.relayout`).

The walk pair is a set of axes ``(count, source stride, destination
stride)`` in order of destination stride, adjacent axes both walks step as
one merged; the innermost destination axis picks the form:

- `Words`: both walks whole words: one mover walk.
- runs (`Runs`): the axis contiguous in the source too. Runs are cut into
  segments inside one vector-core row of destination words; a segment's
  source words are one gathered row (and the next word's row when it is
  rotated), rotated and masked in the vector core, and scattered back as
  whole memory lines (`card.dram.line_bytes`, L1.VER.017: no two moves
  write part of one line). A class (rotation, lane mask) is one loop; a
  width's segments share their gathers and scatters. Segments sharing a
  line are in different layers, each merging over the line as the last
  left it.
- `Pull`: the axis a broadcast of at most a word's lanes of copies (values
  repeated inside a word, `PULL_RUN`): each destination word's lanes from at most
  `PULL_SOURCES` source words, staged by the mover at that word (a word
  walk when the word sequence is one, else a word gather); one vector-core
  program takes each lane from its (source, rotation), a code row a vector
  row says which.
- `Splat`: the axis a longer broadcast: each value a row of copies (a
  statistic broadcast), then runs from those rows.
- transpose (`Exchange`): the axis strided, another contiguous: runs into
  ``[nb, A', B']`` (padding 0), `kernels.transpose`, runs out.

Other walk pairs are `Unplanned` (`kernels.lanes` takes them). The index
lists and masks are data; the kernel depends on the plan's shape only.
"""

import os
from collections import OrderedDict
from dataclasses import dataclass, field, replace

import numpy as np

from kohakutpu.application.kernels import transpose as TP
from kohakutpu.application.kernels import vector as V
from kohakutpu.language import dsl as kt
from kohakutpu.language import machine as MF

#: Vector-core rows a trip takes.
ROWS = 2
#: Index entries a word holds: a list (and a class's rows) from a word.
ENTRIES = 8
#: What `execute` starts scratch buffers with.
POISON = 0x5A5A
PLANS_KEPT = 1024
KERNELS_KEPT = 1024
_plans: OrderedDict = OrderedDict()
_kernels: OrderedDict = OrderedDict()


@dataclass(frozen=True)
class Hw:
    """The machine facts a relayout is planned with."""

    lanes: int  # f16 values a word (vc.lanes)
    row: int  # values a vector-core row (vc.vlmax)
    word: int  # bytes a word (die.word_bytes)
    line: int  # words a memory line (card.dram.line_bytes / word)
    cores: int  # vector cores trips are split over
    levels: int  # levels of a mover walk (mover.levels)
    #: Bytes every scratch buffer starts on: lines and GT4 entries (an entry
    #: written off this boundary is wrong on the card model).
    grain: int
    descriptors: int

    @property
    def row_words(self) -> int:
        return self.row // self.lanes

    @property
    def line_values(self) -> int:
        return self.line * self.lanes


def hw_of(machine, cores: int) -> Hw:
    """`Hw` of a machine file (`language.machine`)."""
    word = machine.die.word_bytes
    line = max(1, machine.get("card.dram.line_bytes") // word)
    return Hw(
        machine.vc.lanes,
        machine.vc.vlmax,
        word,
        line,
        cores,
        machine.mover.levels,
        int(np.lcm(line * word, machine.get("mover.xform.gt4_rows") * word)),
        machine.vc.descriptors,
    )


def hw_for(dev) -> Hw:
    return hw_of(MF.load(dev.machine.board), dev.machine.count("VC"))


# ------------------------------------------------------------------ parts
@dataclass(frozen=True)
class Fill:
    """Words `w0`.. (`words` of them) of `dst` := 0, by the mover."""

    dst: str
    w0: int
    words: int


@dataclass(frozen=True)
class Words:
    """Buffer `src` walked by `levels` (``(count, byte stride)``) from byte
    `at` into `dst` walked by `dlevels` from byte `dat`, by the mover."""

    src: str
    dst: str
    levels: tuple
    at: int
    dlevels: tuple
    dat: int


@dataclass(frozen=True)
class Cls:
    """A class: staging rows `row0`.. (`rows`, whole trips on every core)
    rotated by `rot` (mask row `rmask`), selected under mask row `mask`
    (-1: every lane) over the layer's base."""

    rot: int
    rmask: int
    mask: int
    row0: int
    rows: int


@dataclass(frozen=True)
class Layer:
    """Staging rows scattered together, each to lines no other row of the
    layer writes (line list at `at_d`); lanes outside a row's mask take
    `base`: "zero", or "dst" (the lines as they are). Layer `k` of every
    group runs before layer `k` + 1 of any."""

    k: int
    base: str
    row0: int
    rows: int
    at_d: int
    classes: tuple


@dataclass(frozen=True)
class Group:
    """Rows `words` destination words (whole lines) wide, staging rows
    `row0`.. (`rows`); their source rows' index list at `at_s`, the next
    words' at `at_s2`."""

    words: int
    row0: int
    rows: int
    at_s: int
    at_s2: int | None
    layers: tuple


@dataclass(frozen=True)
class Runs:
    """Segments from word table `src` (`src_words` words; index lists count
    rows of `pitch` words; staged after `margin` words of 0 when set) into
    word table `dst`."""

    src: str
    src_words: int
    pitch: int
    margin: int
    dst: str
    groups: tuple


@dataclass(frozen=True)
class Splat:
    """Words `w0`.. (`count`) of `src`: each value a row of copies of
    `dst`; the mask table's all-ones rows from `ones`."""

    src: str
    w0: int
    count: int
    dst: str
    ones: int


@dataclass(frozen=True)
class Pull:
    """Destination words `w0`.. (`words`, whole vector-core rows; `rows`
    staged, whole trips): slot t's staged row holds, at each destination
    word, a source word of `src` -- `slots[t]` ``("walk", byte levels, byte
    at)`` one mover walk, or ``("list", index at)`` a word gather. The
    vector core takes lane l of word w from lane ``(l + k) mod lanes`` of
    slot t's word where the code row (mask table rows `codes`..) holds ``t
    lanes + k`` (`pairs`: every such code), 0 where it holds `ZERO`, the
    destination's lane (`base` "dst": staged first) where `KEEP`. Words
    `w0`.. (`out` of them) of the result are the destination's."""

    src: str
    dst: str
    w0: int
    words: int
    rows: int
    out: int
    slots: tuple
    codes: int
    pairs: tuple
    base: str
    zeros: bool
    tree: tuple = ()


#: Codes of a `Pull` lane: 0, or the destination's lane as it is.
ZERO, KEEP = -1, -2


@dataclass(frozen=True)
class Exchange:
    """`kernels.transpose` of `src` ``[nb, A, B]`` into `dst` ``[nb, B, A]``
    (its lane masks the mask table's rows from `masks`)."""

    src: str
    dst: str
    nb: int
    A: int
    B: int
    masks: int


@dataclass(frozen=True)
class Shuffle:
    """``dst[b, n, i]`` := ``src[at + n sb + i B + b]`` (b < `B`, n < `nb`, i
    < `A`; rows `Ap` apart): the B word streams of the source (words t, t +
    B, .. into ``sh``) by mover walks, then one vector-core program per b,
    each stream's lanes ``j B + b`` compressed to lanes j (`progs[b]`:
    ``(mask row, ((rotation, mask row), ..), (tap mask row, ..))``), rotated
    to lanes ``t lanes / B`` and selected into place."""

    src: str
    at: int
    nb: int
    sb: int
    A: int
    B: int
    Ap: int
    dst: str
    progs: tuple


@dataclass(frozen=True)
class Plan:
    """A relayout: its parts in order, its scratch buffers ``(name, rows,
    values a row)``, its index lists and mask rows, the source and
    destination port words."""

    parts: tuple
    scratch: tuple
    index: np.ndarray = field(compare=False)
    masks: np.ndarray = field(compare=False)
    src_words: int = 0
    dst_words: int = 0

    def shape(self) -> tuple:
        """What the kernel depends on: all but the tables' contents."""
        return (
            self.parts,
            self.scratch,
            len(self.index),
            tuple(self.masks.shape),
            self.src_words,
            self.dst_words,
        )


class Unplanned(ValueError):
    """A walk pair no form here takes."""


class _Tables:
    """A plan's index lists (fp32, each from a word) and mask rows."""

    def __init__(self, hw: Hw) -> None:
        self.hw = hw
        self.index: list = []
        self.n_index = 0
        self.masks: list = []
        self.keys: dict = {}

    def put(self, ix) -> int:
        ix = np.asarray(ix, np.float32)
        pad = -len(ix) % 8
        if pad:
            ix = np.concatenate([ix, np.full(pad, ix[-1], np.float32)])
        at = self.n_index
        self.index.append(ix)
        self.n_index += len(ix)
        return at

    def mask(self, key, row) -> int:
        got = self.keys.get(key)
        if got is None:
            got = self.keys[key] = len(self.masks)
            self.masks.append(np.asarray(row, np.float16))
        return got

    def rows(self, rows) -> int:
        at = len(self.masks)
        self.masks.extend(np.asarray(r, np.float16) for r in rows)
        return at

    def rot_mask(self, rot: int) -> int:
        """Lanes l < lanes - rot of every word (from the row's word)."""
        lane = np.arange(self.hw.row) % self.hw.lanes
        return self.mask(("rot", rot), lane < self.hw.lanes - rot)

    def lane_mask(self, lo: int, n: int) -> int:
        e = np.zeros(self.hw.row, np.float16)
        e[lo : lo + n] = 1
        return self.mask(("e", lo, n), e)


def canon(walk, dwalk) -> tuple:
    """The walk pair's axes ``(count, source stride, destination stride)``
    of count > 1, outermost first by destination stride, adjacent axes both
    walks step as one merged."""
    axes = [(int(n), int(s), int(d)) for (n, s), (_, d) in zip(walk, dwalk)]
    axes = sorted((a for a in axes if a[0] > 1), key=lambda a: -abs(a[2]))
    out: list = []
    for n, s, d in axes:
        if out and out[-1][1] == n * s and out[-1][2] == n * d:
            out[-1] = (out[-1][0] * n, s, d)
        else:
            out.append((n, s, d))
    return tuple(out)


def positions(axes, at: int, which: int) -> np.ndarray:
    """``at + sum(i_k * stride_k)`` over every index of `axes` in walk
    order; stride `which` (1 source, 2 destination)."""
    idx = np.array([at], np.int64)
    for a in axes:
        step = np.arange(a[0], dtype=np.int64) * a[which]
        idx = (idx[:, None] + step[None, :]).reshape(-1)
    return idx


#: Words one move takes (the stack's move bound, ISA.MV.007): a longer walk
#: is cut into moves, each writing part of the lines at its cuts.
MOVE_WORDS = 16384


def _one_writer(levels, at: int, hw: Hw) -> bool:
    """Whether a word walk (byte `levels` from byte `at`) is the only move
    that writes part of a line: one move, or whole lines only."""
    n = int(np.prod([c for c, _ in levels]))
    line = hw.line * hw.word
    whole = (
        not at % line
        and not (levels[-1][0] * levels[-1][1]) % line
        and all(not s % line for _, s in levels[:-1])
    )
    return n <= MOVE_WORDS or whole


def _fills(dst: str, w0: int, words: int, hw: Hw) -> list:
    """`Fill` parts of words `w0`..: a long one from a line, so that each of
    the moves it is cut into writes whole lines but the first's and last's."""
    head = (-w0) % hw.line if words > MOVE_WORDS else 0
    out = [Fill(dst, w0, head)] if head else []
    return out + [Fill(dst, w0 + head, words - head)]


def _byte_levels(axes, at: int, which: int, hw: Hw) -> tuple | None:
    """Byte levels of a whole-word walk over `axes` (innermost contiguous
    whole words, every other stride whole words), merged; None if not."""
    L = hw.lanes
    if not axes or at % L or axes[-1][which] != 1 or axes[-1][0] % L:
        return None
    lv = []
    for a in axes[:-1]:
        if a[which] % L:
            return None
        lv.append((a[0], a[which] * 2))
    lv.append((axes[-1][0] // L, hw.word))
    out: list = []
    for n, s in lv:
        if out and out[-1][1] == n * s:
            out[-1] = (out[-1][0] * n, s)
        else:
            out.append((n, s))
    return tuple(out) if len(out) <= hw.levels else None


# ------------------------------------------------------------------- runs
def segments(b, L, hw: Hw) -> tuple:
    """``(run, offset, start, count)`` of runs at destination elements `b`,
    `L` elements each (a number or one per run), cut at every vector-core
    row of words (counted from element 0: blocks of whole lines)."""
    rv = hw.row
    b = np.asarray(b, np.int64)
    L = np.broadcast_to(np.asarray(L, np.int64), b.shape)
    k0 = b // rv
    k = (b + L - 1) // rv - k0 + 1
    run = np.repeat(np.arange(len(b)), k)
    c = np.arange(len(run)) - np.repeat(np.cumsum(k) - k, k)
    lo = np.maximum(b[run], (k0[run] + c) * rv)
    hi = np.minimum(b[run] + L[run], (k0[run] + c + 1) * rv)
    return run, lo - b[run], lo, hi - lo


def layers(b, n, span: int) -> np.ndarray:
    """Each segment's layer: segments sharing a `span`-element line in
    different layers (in destination order, the least free layer)."""
    b, n = np.asarray(b, np.int64), np.asarray(n, np.int64)
    first, last = b // span, (b + n - 1) // span
    out = np.zeros(len(b), np.int64)
    held: dict = {}
    for i in np.argsort(b, kind="stable").tolist():
        f, e = int(first[i]), int(last[i])
        used = held.get(f, set()) | held.get(e, set())
        k = 0
        while k in used:
            k += 1
        out[i] = k
        held.setdefault(f, set()).add(k)
        held.setdefault(e, set()).add(k)
    return out


def runs(q, rot, b, n, *, src, src_words, pitch, dst, zero, tables, hw) -> Runs:
    """`Runs` of segments into destination elements `b`.. (`n` each): the
    segment's lane l of destination word w reads lane ``(l + rot) mod
    lanes`` of source word ``w + q`` (the next word past the lane's end of
    the word) -- with `pitch` > 1, row `q` of the table, unrotated. A
    segment is scattered as its whole lines; `zero`: the lanes no segment
    writes are 0 (first layer), else kept."""
    lanes, lw = hw.lanes, hw.line
    q, rot, b, n = (np.asarray(x, np.int64) for x in (q, rot, b, n))
    l0 = b // hw.line_values * lw
    wd = ((b + n - 1) // lanes // lw + 1) * lw - l0
    lo = b - l0 * lanes
    full = (lo == 0) & (n == wd * lanes)
    lay = layers(b, n, hw.line_values)
    if pitch == 1:
        s1 = l0 + q
        s2 = np.where(rot != 0, s1 + 1, s1)
        need = max(0, -int(s1.min()), int((s2 + wd).max()) - src_words) if len(b) else 0
        margin = margin_words(need, hw)
    else:
        s1 = s2 = q
        margin = 0
    unit = np.lcm(ROWS * hw.cores, ENTRIES)
    groups, row = [], 0
    for w in sorted(set(wd.tolist())):
        in_w = np.flatnonzero(wd == w)
        s_rows, s2_rows, out, g0 = [], [], [], row
        for k in sorted(set(lay[in_w].tolist())):
            by: dict = {}
            for i in in_w[lay[in_w] == k].tolist():
                rm = tables.rot_mask(int(rot[i])) if rot[i] else -1
                em = -1 if full[i] else tables.lane_mask(int(lo[i]), int(n[i]))
                by.setdefault((int(rot[i]), rm, em), []).append(i)
            classes, dlist, r0 = [], [], row
            for (r, rm, em), ix in sorted(by.items()):
                ix = ix + [ix[-1]] * (-len(ix) % unit)
                classes.append(Cls(r, rm, em, row, len(ix)))
                s_rows += (s1[ix] + margin).tolist()
                s2_rows += (s2[ix] + margin).tolist()
                dlist += (l0[ix] // lw).tolist()
                row += len(ix)
            base = "zero" if zero and k == 0 else "dst"
            out.append(Layer(k, base, r0, row - r0, tables.put(dlist), tuple(classes)))
        rotated = bool((rot[in_w] != 0).any())
        groups.append(
            Group(
                int(w),
                g0,
                row - g0,
                tables.put(s_rows),
                tables.put(s2_rows) if rotated else None,
                tuple(out),
            )
        )
    return Runs(src, int(src_words), int(pitch), margin, dst, tuple(groups))


def _run_parts(a, b, L, *, src, src_words, dst, zero, tables, hw) -> Runs:
    """`Runs` of `L`-element runs from source elements `a` (a word table)
    to destination elements `b`."""
    lanes = hw.lanes
    run, off, sb, sn = segments(b, L, hw)
    delta = np.asarray(a, np.int64)[run] + off - sb
    rot = delta % lanes
    return runs(
        (delta - rot) // lanes, rot, sb, sn, src=src, src_words=src_words, pitch=1,
        dst=dst, zero=zero, tables=tables, hw=hw,
    )  # fmt: skip


def _in_order(r: Runs) -> list:
    """``(group, layer)`` in the order they run: by layer, then width."""
    return sorted(
        ((g, lay) for g in r.groups for lay in g.layers), key=lambda gl: gl[1].k
    )


def margin_words(need: int, hw: Hw) -> int:
    """Words of 0 staged around a table: whole lines, so the staged copy,
    cut into moves, writes whole lines but its first and last."""
    return -(-need // hw.line) * hw.line


def margin_name(r: Runs) -> str:
    """The staged copy of a table with words of 0 around it: one buffer a
    table, its copy writing part of its first and last lines (L1.VER.017:
    no other move may)."""
    return f"rm_{r.src}"


def _staging(r: Runs, hw: Hw) -> list:
    rows = sum(g.rows for g in r.groups)
    out = [("rs", rows, hw.row), ("ro", rows, hw.row)] if rows else []
    if any(g.at_s2 is not None for g in r.groups):
        out.append(("rs2", rows, hw.row))
    if any(x.base == "dst" for g in r.groups for x in g.layers):
        out.append(("rd", rows, hw.row))
    if r.margin:
        words = -(-(r.src_words + 2 * r.margin) // hw.line) * hw.line
        out.append((margin_name(r), words, hw.lanes))
    return out


# --------------------------------------------------------------- planning
def plan(
    src_n: int, walk, at: int, dwalk, dat: int, m: int, keep: bool, hw: Hw, z0: int = 0
) -> Plan:
    """The relayout of `m` destination elements (a port from a line, whole
    words) from element `at` of `src_n` source elements walked by `walk`
    into the elements `dwalk` reaches from `dat`; every other element of
    ``[z0, m)`` 0 (with `keep`, as it is), those before `z0` kept. Raises
    `Unplanned` for a walk pair no form here takes."""
    key = (
        int(src_n),
        tuple((int(n), int(s)) for n, s in walk),
        int(at),
        tuple((int(n), int(s)) for n, s in dwalk),
        int(dat),
        int(m),
        bool(keep),
        hw,
        int(z0),
    )
    tree = os.environ.get("KTPU_PULL_TREE", "1") == "1"
    cache_key = (key, tree)
    got = _plans.get(cache_key)
    if got is None:
        try:
            got = _plan(*key)
            if tree:
                got = tree_pull(got, hw)
        except Unplanned as e:
            got = e
        if PLANS_KEPT > 0:
            _plans[cache_key] = got
            while len(_plans) > PLANS_KEPT:
                _plans.popitem(last=False)
    else:
        _plans.move_to_end(cache_key)
    if isinstance(got, Unplanned):
        raise got
    return got


def tree_pull(pl: Plan, hw: Hw) -> Plan:
    """Predecode gather codes into shared selector bits for a balanced tree.

    Original code rows remain the independent executor's source of truth. The
    tree selects raw rotated payloads; it never adds or multiplies payloads.
    """
    masks = [pl.masks]
    next_row = len(pl.masks)
    parts = []
    for part in pl.parts:
        if not isinstance(part, Pull) or len(part.pairs) < 2:
            parts.append(part)
            continue
        code = pl.masks[part.codes : part.codes + part.rows].astype(np.int64)
        bits = (len(part.pairs) - 1).bit_length()
        # Each source/mask/output view consumes a target descriptor. Retain
        # serial selection when the additional mask streams do not fit.
        views = (
            len(part.slots)
            + bits
            + int((code < 0).any())
            + int(part.zeros)
            + int(part.base == "dst")
            + 1
        )
        if views > hw.descriptors:
            parts.append(part)
            continue
        index = np.searchsorted(part.pairs, code)
        selectors = []
        for bit in range((len(part.pairs) - 1).bit_length()):
            masks.append(((index >> bit) & 1).astype(np.float16))
            selectors.append(next_row)
            next_row += part.rows
        valid = -1
        if (code < 0).any():
            masks.append((code >= 0).astype(np.float16))
            valid = next_row
            next_row += part.rows
        zero = -1
        if part.zeros:
            masks.append((code == ZERO).astype(np.float16))
            zero = next_row
            next_row += part.rows
        parts.append(replace(part, tree=(tuple(selectors), valid, zero)))
    return replace(pl, parts=tuple(parts), masks=np.concatenate(masks))


def _plan(src_n, walk, at, dwalk, dat, m, keep, hw: Hw, z0) -> Plan:
    lanes, lv = hw.lanes, hw.line_values
    sw = -(-src_n // lanes)
    dw = -(-m // lv) * hw.line
    axes = canon(walk, dwalk)
    spos, dpos = positions(axes, at, 1), positions(axes, dat, 2)
    if len(spos) and (spos.min() < 0 or spos.max() >= src_n):
        raise ValueError(f"source elements outside 0..{src_n}")
    if len(dpos) and (dpos.min() < z0 or dpos.max() >= m):
        raise ValueError(f"destination elements outside {z0}..{m}")
    if not axes:
        raise Unplanned("one element")
    if len(np.unique(dpos)) != len(dpos):
        raise Unplanned("a destination element written twice")
    t = _Tables(hw)
    parts: list = []
    if not keep and len(dpos) < m - z0:
        parts += _fills("dst", z0 // lanes, -(-m // lanes) - z0 // lanes, hw)
    # Lanes no segment writes may be 0 where the port is the zero range.
    zero = not keep and not z0 and m == dw * lanes
    # A walk may write part of a line that only a fill of whole lines writes.
    whole = not parts or not (z0 % lv or m % lv)
    room = None if keep else (z0, m)
    sl = _byte_levels(axes, at, 1, hw)
    dl = _byte_levels(axes, dat, 2, hw)
    if sl is not None and dl is not None and whole and _one_writer(dl, dat * 2, hw):
        return _done(
            (*parts, Words("src", "dst", sl, at * 2, dl, dat * 2)), [], t, sw, dw
        )
    n, s, d = axes[-1]
    if d != 1:
        # Every element its own destination run: a transpose whose
        # destination-contiguous side is one element.
        j = next((k for k, x in enumerate(axes) if x[1] == 1), None)
        if j is None:
            raise Unplanned("no axis is contiguous on either side")
        return _transpose(
            (*axes, (1, 0, 1)), j, at, dat, sw, dw, zero, whole, parts, t, hw, room
        )
    rows = axes[:-1]
    if s == 1:
        r = _run_parts(
            positions(rows, at, 1), positions(rows, dat, 2), n, src="src",
            src_words=sw, dst="dst", zero=zero, tables=t, hw=hw,
        )  # fmt: skip
        return _done((*parts, r), _staging(r, hw), t, sw, dw)
    if s == 0:
        if n <= (lanes if PULL_RUN is None else PULL_RUN):
            try:
                return _pull(axes, at, dat, sw, z0, m, keep, t, hw)
            except Unplanned:
                t = _Tables(hw)
        return _splat(rows, n, at, dat, sw, dw, zero, parts, t, hw)
    j = next((k for k, x in enumerate(rows) if x[1] == 1), None)
    if j is None:
        # A strided read: a transpose whose source-contiguous side is one
        # element.
        rows, j = (*rows, (1, 1, 0)), len(rows)
    else:
        try:
            got = _shuffle(rows, j, n, s, at, dat, sw, dw, zero, whole, parts, t, hw)
        except Unplanned:
            got = None
        if got is not None:
            return got
        t = _Tables(hw)
    return _transpose(
        (*rows, axes[-1]), j, at, dat, sw, dw, zero, whole, parts, t, hw, room
    )


def _done(parts, scratch, t: _Tables, sw: int, dw: int) -> Plan:
    index = np.concatenate(t.index) if t.index else np.zeros(8, np.float32)
    masks = np.stack(t.masks) if t.masks else np.zeros((1, t.hw.row), np.float16)
    merged: dict = {}
    for name, rows, width in scratch:
        merged[name] = (max(rows, merged.get(name, (0,))[0]), width)
    return Plan(
        tuple(parts),
        tuple((k, r, w) for k, (r, w) in sorted(merged.items())),
        index,
        masks,
        sw,
        dw,
    )


def _splat(rows, n, at, dat, sw, dw, zero, parts, t: _Tables, hw: Hw) -> Plan:
    """Runs of `n` copies of one value: each value a row of copies (row v
    of ``sp`` holds the value at element ``lanes * w0 + v``), then runs
    from that row (any of its words is the value's)."""
    lanes = hw.lanes
    vals = positions(rows, at, 1)
    b = positions(rows, dat, 2)
    w0 = int(vals.min()) // lanes
    count = int(vals.max()) // lanes - w0 + 1
    sp = Splat("src", w0, count, "sp", t.rows(np.ones((lanes, hw.row))))
    run, _, sb, sn = segments(b, n, hw)
    q = vals[run] - w0 * lanes
    r = runs(
        q, np.zeros_like(q), sb, sn, src="sp", src_words=count * lanes * hw.row_words,
        pitch=hw.row_words, dst="dst", zero=zero, tables=t, hw=hw,
    )  # fmt: skip
    scratch = [("sp", count * lanes, hw.row), *_staging(r, hw)]
    return _done((*parts, sp, r), scratch, t, sw, dw)


#: Copies a broadcast axis makes at most for a `Pull` to take it (None: a
#: word's lanes); a longer one is a `Splat`. Measured on the card model over
#: the LeNet / ViT broadcasts: a repeat inside a word costs a `Splat` a row
#: a value (2,126,757 cycles for two of them), a `Pull` 340,739.
PULL_RUN: int | None = None
#: Source words a destination word's lanes come from at most in a `Pull`.
PULL_SOURCES = 2


def word_levels(seq, levels: int) -> tuple | None:
    """``(at, ((count, stride), ..))``, outermost first and at most `levels`
    of them, whose walk visits the integers `seq` in order; None when no
    such walk is."""
    seq = np.asarray(seq, np.int64)
    out: list = []
    while len(seq) > 1:
        n, d = len(seq), np.diff(seq)
        bad = np.flatnonzero(d != d[0])
        run = int(bad[0]) + 1 if len(bad) else n
        got = None
        for c in range(min(run, n), 1, -1):
            if n % c:
                continue
            a = seq.reshape(-1, c)
            if (a - a[:, :1] == a[0] - a[0, 0]).all():
                got = c
                break
        if got is None:
            return None
        out.insert(0, (got, int(d[0])))
        seq = seq.reshape(-1, got)[:, 0]
    if len(out) > levels:
        return None
    return int(seq[0]), tuple(out) or ((1, 0),)


def pull_rotation(src_lane, lane, lanes: int):
    """The rotation (`kt.xrot`: lane l takes lane l + k) that brings a
    source word's `src_lane` to `lane`."""
    return (src_lane - lane) % lanes


def _pull(axes, at, dat, sw, z0, m, keep, t: _Tables, hw: Hw) -> Plan:
    """`Pull` of every destination vector-core row the walk pair writes:
    each destination word's lanes from at most `PULL_SOURCES` source words
    (else `Unplanned`), the zero range's other lanes 0, the rest kept; the
    zero range outside those rows filled."""
    lanes, rv, rw = hw.lanes, hw.row, hw.row_words
    spos, dpos = positions(axes, at, 1), positions(axes, dat, 2)
    dw = -(-m // hw.line_values) * hw.line
    r0, r1 = int(dpos.min()) // rv, int(dpos.max()) // rv + 1
    w0, words = r0 * rw, (r1 - r0) * rw
    out = min(r1 * rw, dw) - w0
    e = w0 * lanes + np.arange(words * lanes)
    src_of = np.full(words * lanes, -1, np.int64)
    src_of[dpos - w0 * lanes] = spos
    code = np.where((not keep) & (e >= z0) & (e < m), ZERO, KEEP)
    sword = np.where(src_of >= 0, src_of // lanes, -1).reshape(words, lanes)
    left = (src_of >= 0).reshape(words, lanes)
    slot = np.zeros((words, lanes), np.int64)
    seqs = []
    for k in range(PULL_SOURCES):
        has = left.any(axis=1)
        if not has.any():
            break
        first = sword[np.arange(words), np.argmax(left, axis=1)]
        mine = left & (sword == first[:, None])
        slot[mine] = k
        left &= ~mine
        # Words with no k-th source take the previous word's (any source
        # word does: none of their lanes reads it).
        idx = np.where(has, np.arange(words), 0)
        np.maximum.accumulate(idx, out=idx)
        seq = first[idx]
        seqs.append(np.where(has[idx], seq, first[np.argmax(has)]))
    if left.any():
        raise Unplanned(f"a destination word from over {PULL_SOURCES} source words")
    pulled = src_of >= 0
    rot = pull_rotation(src_of % lanes, e % lanes, lanes)
    code = np.where(pulled, slot.reshape(-1) * lanes + rot, code)
    base = "dst" if (code == KEEP).any() else "zero"
    zeros = base == "dst" and bool((code == ZERO).any())
    rows = -(-(r1 - r0) // ROWS) * ROWS
    pad = (rows - (r1 - r0)) * rv
    code = np.concatenate([code, np.full(pad, KEEP if base == "dst" else ZERO)])
    codes = t.rows(code.reshape(rows, rv))
    slots, scratch = [], []
    for k, seq in enumerate(seqs):
        lv = word_levels(seq, hw.levels)
        if lv is not None:
            slots.append(
                ("walk", tuple((c, s * hw.word) for c, s in lv[1]), lv[0] * hw.word)
            )
        else:
            slots.append(("list", t.put(seq)))
            scratch.append((f"pw{k}", words, lanes))
        scratch.append((f"ps{k}", rows, rv))
    if base == "dst":
        scratch.append(("pd", rows, rv))
    scratch.append(("po", rows, rv))
    parts: list = []
    if not keep:
        a, b = z0 // lanes, -(-m // lanes)
        if z0 % lanes and not w0 <= a < w0 + out:
            raise Unplanned("a zero range from inside a word outside the rows")
        if a < w0:
            parts += _fills("dst", a, w0 - a, hw)
        if b > w0 + out:
            parts += _fills("dst", w0 + out, b - w0 - out, hw)
    pairs = tuple(sorted(int(c) for c in set(code[code >= 0].tolist())))
    pl = Pull(
        "src", "dst", w0, words, rows, out, tuple(slots), codes, pairs, base, zeros
    )
    return _done((*parts, pl), scratch, t, sw, dw)


def _transpose(
    axes, j, at, dat, sw, dw, zero, whole, parts, t: _Tables, hw: Hw, room=None
) -> Plan:
    """Axis `j` contiguous in the source, the innermost destination axis
    (A elements) strided there: ``tx[nb, A', B']`` from the source (padding
    0), the exchange into ``ty[nb, B', A']``, the destination from it; a
    side of whole words one walk, else runs. `room`: the zero range
    ``(z0, m)`` (None when kept)."""
    lanes = hw.lanes
    A, sA, _ = axes[-1]
    B, _, dB = axes[j]
    batch = [x for k, x in enumerate(axes[:-1]) if k != j]
    nb = int(np.prod([x[0] for x in batch])) if batch else 1
    Ao = _extend(batch, B, dB, A, dat, room, hw)
    Ap, Bp = max(Ao, -(-A // lanes) * lanes), -(-B // lanes) * lanes
    xw = -(-nb * Ap * Bp // lanes // hw.line) * hw.line
    parts = list(parts)
    # tx is scattered into (rows of whole lines), ty gathered from (words);
    # their padding is never read out, but for rows the out side writes as
    # zeros (tx filled first).
    scratch = [("tx", xw // hw.line, hw.line_values), ("ty", xw, lanes)]
    if Ao > A:
        if nb * B * Ao == room[1] - room[0]:
            # The out side writes every element of the zero range.
            parts = [p for p in parts if not (isinstance(p, Fill) and p.dst == "dst")]
            whole = True
        parts += _fills("tx", 0, xw, hw)
    sl = _byte_levels(
        (*((x[0], x[1], 0) for x in batch), (A, sA, 0), (B, 1, 0)), at, 1, hw
    )
    dl = _byte_levels(_dense_axes(batch, A, B, Ap, Bp), 0, 2, hw)
    if sl is not None and dl is not None and _one_writer(dl, 0, hw):
        parts.append(Words("src", "tx", sl, at * 2, dl, 0))
    else:
        a = (positions(batch, at, 1)[:, None] + np.arange(A)[None, :] * sA).reshape(-1)
        b = (np.arange(nb * A) // A * Ap + np.arange(nb * A) % A) * Bp
        r = _run_parts(
            a, b, B, src="src", src_words=sw, dst="tx", zero=True, tables=t, hw=hw
        )
        parts.append(r)
        scratch += _staging(r, hw)
    parts.append(Exchange("tx", "ty", nb, Ap, Bp, t.rows(TP.masks())))
    db = positions(batch, dat, 2)
    dl = _byte_levels(
        (*((x[0], 0, x[2]) for x in batch), (B, 0, dB), (Ao, 0, 1)), dat, 2, hw
    )
    sl = _byte_levels(_dense_axes(batch, B, Ao, Bp, Ap), 0, 2, hw)
    if dl is not None and sl is not None and whole and _one_writer(dl, dat * 2, hw):
        parts.append(Words("ty", "dst", sl, 0, dl, dat * 2))
    else:
        a = (np.arange(nb * B) // B * Bp + np.arange(nb * B) % B) * Ap
        b = (db[:, None] + np.arange(B)[None, :] * dB).reshape(-1)
        r = _run_parts(
            a, b, A, src="ty", src_words=xw, dst="dst", zero=zero, tables=t, hw=hw
        )
        parts.append(r)
        scratch += _staging(r, hw)
    return _done(tuple(parts), scratch, t, sw, dw)


def _extend(batch, B: int, dB: int, A: int, dat: int, room, hw: Hw) -> int:
    """The row length a transpose's out side writes: `A`, or `A` rounded up
    to whole lines when each of the ``[batch.., B]`` destination rows (`dB`
    apart) starts on a line and the elements it adds are in the zero range
    `room` (``(z0, m)``, None when kept) and no row's elements -- then
    written as zeros, the row whole lines."""
    lv = hw.line_values
    Ae = -(-A // lv) * lv
    if room is None or Ae == A:
        return A
    rows = (
        positions(tuple((x[0], 0, x[2]) for x in batch), dat, 2)[:, None]
        + np.arange(B)[None, :] * dB
    ).reshape(-1)
    if (rows % lv).any():
        return A
    span = (rows[:, None] + np.arange(Ae)[None, :]).reshape(-1)
    if span.min() < room[0] or span.max() >= room[1]:
        return A
    if len(np.unique(span)) != len(span):
        return A
    return Ae


#: Contiguous source sides a lane shuffle takes: a program's B stream
#: loads, compress and tap masks and store fit a core's 8 memory views.
SHUFFLE = (2,)


def compress(B: int, b: int, lanes: int) -> list:
    """``[(k, mask)]``: rotation stages (lane l from lane l + k where the
    mask is set) taking lane ``j B + b`` to lane j for j < lanes / B,
    least shift first; `Unplanned` when a stage would overwrite a lane
    still wanted."""
    want = {j * B + b: j for j in range(lanes // B)}
    at = {e: e for e in want}
    out, k = [], 1
    while k < lanes:
        moves = {e: p - k for e, p in at.items() if (p - want[e]) & k}
        held = {p for e, p in at.items() if e not in moves}
        if any(q in held or q < 0 for q in moves.values()):
            raise Unplanned("a lane compress that overwrites itself")
        if moves:
            mask = np.zeros(lanes, bool)
            mask[list(moves.values())] = True
            out.append((k, mask))
            at.update(moves)
        k *= 2
    return out


def _shuffle(rows, j, n, s, at, dat, sw, dw, zero, whole, parts, t: _Tables, hw: Hw):
    """A transpose whose contiguous source side (B elements) is one of
    `SHUFFLE` and the other (A = `n`, stride B) whole words: `Shuffle`
    into ``ty``, then the destination from it; None when it is not one."""
    lanes = hw.lanes
    B = rows[j][0]
    batch = [x for k, x in enumerate(rows) if k != j]
    if B not in SHUFFLE or s != B or n % lanes or at % lanes:
        return None
    if any(x[1] % lanes for x in batch) or len(batch) > 1:
        return None
    nb, sb = (batch[0][0], batch[0][1]) if batch else (1, 0)
    # A row of a stream: whole trips of vector-core rows.
    Ap = -(-n // (ROWS * hw.row)) * ROWS * hw.row
    g, progs = lanes // B, []
    lane = np.arange(hw.row) % lanes
    for b in range(B):
        stages = compress(B, b, lanes)
        m0 = t.rows([np.tile(m, hw.row // lanes) for _, m in stages])
        taps = t.rows([(lane >= tt * g) & (lane < (tt + 1) * g) for tt in range(1, B)])
        progs.append(
            (
                m0,
                tuple((k, m0 + i) for i, (k, _) in enumerate(stages)),
                tuple(range(taps, taps + B - 1)),
            )
        )
    rv = nb * Ap // hw.row
    shp = Shuffle("src", at, nb, sb, n, B, Ap, "ty", tuple(progs))
    parts = [*parts, shp]
    scratch = [("sh", B * rv, hw.row), ("ty", B * rv, hw.row)]
    # ty row (b, n) at element (b nb + n) Ap; the destination's (batch, b, i).
    d_axes = (*((x[0], 0, x[2]) for x in batch), (B, 0, rows[j][2]), (n, 0, 1))
    s_axes = (*((x[0], 0, Ap) for x in batch), (B, 0, nb * Ap), (n, 0, 1))
    dl = _byte_levels(d_axes, dat, 2, hw)
    sl = _byte_levels(s_axes, 0, 2, hw)
    if dl is not None and sl is not None and whole and _one_writer(dl, dat * 2, hw):
        parts.append(Words("ty", "dst", sl, 0, dl, dat * 2))
        return _done(tuple(parts), scratch, t, sw, dw)
    # Runs read a word table: ty copied into one.
    tw = B * rv * hw.row_words
    parts.append(Words("ty", "tz", ((tw, hw.word),), 0, ((tw, hw.word),), 0))
    a = positions(s_axes[:-1], 0, 2)
    b = positions(d_axes[:-1], dat, 2)
    r = _run_parts(
        a, b, n, src="tz", src_words=tw, dst="dst", zero=zero, tables=t, hw=hw
    )
    parts.append(r)
    scratch += [("tz", tw, lanes), *_staging(r, hw)]
    return _done(tuple(parts), scratch, t, sw, dw)


def _dense_axes(batch, X, Y, Xp, Yp) -> tuple:
    """Axes (destination strides) of ``[batch.., X, Y]`` held in a dense
    ``[nb, Xp, Yp]`` block."""
    out, stride = [], Xp * Yp
    for x in reversed(batch):
        out.insert(0, (x[0], 0, stride))
        stride *= x[0]
    return (*out, (X, 0, Yp), (Y, 0, 1))


# ------------------------------------------------------------- execution
def execute(pl: Plan, src: np.ndarray, dst: np.ndarray, hw: Hw) -> np.ndarray:
    """What the kernel computes, part by part, on numpy bit patterns:
    `src` and `dst` uint16 elements; returns the new `dst`."""
    lanes = hw.lanes
    bufs = {
        "src": np.zeros(pl.src_words * lanes, np.uint16),
        "dst": np.zeros(pl.dst_words * lanes, np.uint16),
    }
    bufs["src"][: len(src)] = src
    bufs["dst"][: len(dst)] = dst
    for name, rows, width in pl.scratch:
        # Scratch holds whatever it held: nothing may read it unwritten.
        bufs[name] = np.full(rows * width, POISON, np.uint16)
    masks = pl.masks.astype(np.float32) != 0
    for part in pl.parts:
        if isinstance(part, Fill):
            bufs[part.dst][part.w0 * lanes : (part.w0 + part.words) * lanes] = 0
        elif isinstance(part, Words):
            _x_words(part, bufs)
        elif isinstance(part, Splat):
            vals = bufs[part.src][part.w0 * lanes : (part.w0 + part.count) * lanes]
            bufs[part.dst][: len(vals) * hw.row] = np.repeat(vals, hw.row)
        elif isinstance(part, Exchange):
            x = bufs[part.src][: part.nb * part.A * part.B]
            y = x.reshape(part.nb, part.A, part.B).transpose(0, 2, 1).reshape(-1)
            bufs[part.dst][: len(y)] = y
        elif isinstance(part, Shuffle):
            _x_shuffle(part, bufs, masks, hw)
        elif isinstance(part, Pull):
            _x_pull(part, bufs, pl, hw)
        else:
            _x_runs(part, bufs, pl.index.astype(np.int64), masks, hw)
    return bufs["dst"][: len(dst)]


def _x_words(w: Words, bufs) -> None:
    def elements(levels, at):
        idx = np.array([at // 2], np.int64)
        for n, s in levels[:-1]:
            idx = (idx[:, None] + np.arange(n)[None, :] * (s // 2)).reshape(-1)
        n, s = levels[-1]
        return (idx[:, None] + np.arange(n * s // 2)[None, :]).reshape(-1)

    bufs[w.dst][elements(w.dlevels, w.dat)] = bufs[w.src][elements(w.levels, w.at)]


def walk_words(levels, at: int, word: int) -> np.ndarray:
    """The word indices a byte walk (`levels` from byte `at`) visits."""
    idx = np.array([at // word], np.int64)
    for n, s in levels:
        idx = (idx[:, None] + np.arange(n)[None, :] * (s // word)).reshape(-1)
    return idx


def _x_pull(p: Pull, bufs, pl: Plan, hw: Hw) -> None:
    lanes, rv = hw.lanes, hw.row
    total = p.rows * rv // lanes
    src = bufs[p.src].reshape(-1, lanes)
    staged = []
    for how in p.slots:
        if how[0] == "walk":
            wds = walk_words(how[1], how[2], hw.word)
        else:
            wds = pl.index[how[1] : how[1] + p.words].astype(np.int64)
        s = np.zeros((total, lanes), np.uint16)  # padding rows: filled 0
        s[: p.words] = src[wds]
        staged.append(s)
    dst = bufs[p.dst].reshape(-1, lanes)
    acc = np.zeros((total, lanes), np.uint16)
    if p.base == "dst":
        acc[: p.out] = dst[p.w0 : p.w0 + p.out]
    code = pl.masks[p.codes : p.codes + p.rows].astype(np.float32).astype(np.int64)
    code = code.reshape(total, lanes)
    lane = np.arange(lanes)
    for c in p.pairs:
        k, r = divmod(c, lanes)
        sel = code == c
        acc[sel] = staged[k][:, (lane + r) % lanes][sel]
    if p.zeros:
        acc[code == ZERO] = 0
    dst[p.w0 : p.w0 + p.out] = acc[: p.out]


def _x_shuffle(sh: Shuffle, bufs, masks, hw: Hw) -> None:
    lanes, B = hw.lanes, sh.B
    g, rv = lanes // B, sh.nb * sh.Ap // hw.row
    src, s, y = bufs[sh.src], bufs["sh"], bufs[sh.dst]
    words = sh.A // lanes
    for t in range(B):
        for n in range(sh.nb):
            for w in range(words):
                q = (sh.at + n * sh.sb) // lanes + w * B + t
                d = (t * rv * hw.row + n * sh.Ap) // lanes + w
                s[d * lanes : (d + 1) * lanes] = src[q * lanes : (q + 1) * lanes]
    for b, (m0, stages, taps) in enumerate(sh.progs):
        for w in range(rv * hw.row_words):
            acc = None
            for t in range(B):
                q = t * rv * hw.row_words + w
                v = s[q * lanes : (q + 1) * lanes]
                for k, m in stages:
                    v = np.where(masks[m][:lanes], np.roll(v, -k), v)
                if t:
                    v = np.roll(v, t * g)
                    acc = np.where(masks[taps[t - 1]][:lanes], v, acc)
                else:
                    acc = v
            q = b * rv * hw.row_words + w
            y[q * lanes : (q + 1) * lanes] = acc


def _x_runs(r: Runs, bufs, index, masks, hw: Hw) -> None:
    lanes, row = hw.lanes, hw.row
    table = bufs[r.src]
    if r.margin:
        staged = np.full(len(table) + 2 * r.margin * lanes, POISON, np.uint16)
        staged[r.margin * lanes : r.margin * lanes + len(table)] = table
        table = staged
    dst = bufs[r.dst]
    stride = r.pitch * lanes

    def rows_of(at, n, w):
        out = np.zeros((n, row), np.uint16)
        for k in range(n):
            q = int(index[at + k])
            out[k, : w * lanes] = table[q * stride : q * stride + w * lanes]
        return out

    total = sum(g.rows for g in r.groups)
    s = np.zeros((total, row), np.uint16)
    s2 = np.zeros((total, row), np.uint16)
    for g in r.groups:
        s[g.row0 : g.row0 + g.rows] = rows_of(g.at_s, g.rows, g.words)
        if g.at_s2 is not None:
            s2[g.row0 : g.row0 + g.rows] = rows_of(g.at_s2, g.rows, g.words)
    rot = (np.arange(lanes)[None, :] + np.arange(lanes)[:, None]) % lanes
    lv = hw.line_values
    for g, lay in _in_order(r):
        w = g.words * lanes
        dl = index[lay.at_d : lay.at_d + lay.rows].tolist()
        d = np.zeros((lay.rows, row), np.uint16)
        for j, q in enumerate(dl):
            d[j, :w] = dst[q * lv : q * lv + w]
        o = np.zeros((lay.rows, row), np.uint16)
        for c in lay.classes:
            for i in range(c.row0, c.row0 + c.rows):
                v = s[i]
                if c.rot:
                    rv = v.reshape(-1, lanes)[:, rot[c.rot]].reshape(-1)
                    rv2 = s2[i].reshape(-1, lanes)[:, rot[c.rot]].reshape(-1)
                    v = np.where(masks[c.rmask], rv, rv2)
                if c.mask >= 0:
                    base = d[i - lay.row0] if lay.base == "dst" else 0
                    v = np.where(masks[c.mask], v, base)
                o[i - lay.row0] = v
        for j, q in enumerate(dl):
            dst[q * lv : q * lv + w] = o[j, :w]


# ----------------------------------------------------------- the kernel
def relayout_kernel(pl: Plan, hw: Hw):
    """Ports ``src`` (f16 as words), ``dst`` (f16 as words, from a line),
    ``index`` (`pl.index`, fp32), ``mask`` (`pl.masks`). Returns
    ``(kernel, given)``."""
    kkey = (pl.shape(), hw, V.split, kt.Kernel.build)
    hit = _kernels.get(kkey)
    if hit is not None:
        _kernels.move_to_end(kkey)
        return hit
    lanes, lines = hw.lanes, pl.dst_words // hw.line
    # The destination's rows are lines: a scatter's rows provably own them.
    ports = {
        "src": kt.In(pl.src_words, lanes),
        "dst": kt.Out(lines, hw.line_values),
        "index": kt.In(len(pl.index), dtype="f32"),
        "mask": kt.In(*pl.masks.shape),
    }
    given = {
        "src": kt.TensorSpec.make((pl.src_words, lanes)),
        "dst": kt.TensorSpec.make((lines, hw.line_values)),
        "index": kt.TensorSpec.make((len(pl.index),), "f32"),
        "mask": kt.TensorSpec.make(tuple(pl.masks.shape)),
    }

    def body(src, dst, index, mask):
        bufs = {"src": src, "dst": dst}
        for name, rows, width in pl.scratch:
            bufs[name] = kt.buffer((scratch_rows(rows, width, hw), width), "f16")
        for part in pl.parts:
            _emit(part, bufs, index, mask, hw)

    # The relayout kernels' family name (`lanes_`), whichever form.
    name = V.name("lanes", "relayout", pl.shape(), hw)
    got = (kt.Kernel.build(name, body, ports), given)
    if KERNELS_KEPT > 0:
        _kernels[kkey] = got
        while len(_kernels) > KERNELS_KEPT:
            _kernels.popitem(last=False)
    return got


def emit_steps(plans: tuple, src, tables: dict, prefix: str, hw: Hw, destination=None):
    """Statements of `plans` inside the kernel being traced, each into a
    scratch of lines: the first reading `src` (a word table), each next the
    one before (its lines copied into a word table); `tables` maps
    ``{prefix}index{i}`` / ``{prefix}mask{i}`` to their ports. Every buffer on
    the grain (L2.REL.013). Returns the last plan's lines buffer."""
    lanes, word = hw.lanes, hw.word

    def scratch(n: int, width: int):
        return kt.buffer((scratch_rows(n, width, hw), width), "f16")

    cur = src
    for i, p in enumerate(plans):
        if i:
            w = scratch(p.src_words, lanes)
            kt.copy(w, cur, walk=((p.src_words, word),), dwalk=((p.src_words, word),))
            cur = w
        dst = (
            destination
            if destination is not None and i == len(plans) - 1
            else scratch(p.dst_words // hw.line, hw.line_values)
        )
        bufs = {"src": cur, "dst": dst}
        for name, n, width in p.scratch:
            bufs[name] = scratch(n, width)
        index, mask = tables[f"{prefix}index{i}"], tables[f"{prefix}mask{i}"]
        for part in p.parts:
            _emit(part, bufs, index, mask, hw)
        cur = dst
    return cur


def step_ports(plans: tuple, prefix: str) -> tuple:
    """``(ports, given)`` of `plans`' tables for `emit_steps`."""
    ports, given = {}, {}
    for i, p in enumerate(plans):
        ports[f"{prefix}index{i}"] = kt.In(len(p.index), dtype="f32")
        ports[f"{prefix}mask{i}"] = kt.In(*p.masks.shape)
        given[f"{prefix}index{i}"] = kt.TensorSpec.make((len(p.index),), "f32")
        given[f"{prefix}mask{i}"] = kt.TensorSpec.make(tuple(p.masks.shape))
    return ports, given


def scratch_rows(rows: int, width: int, hw: Hw) -> int:
    """`rows` of `width` f16 rounded up to whole `hw.grain` bytes: the
    kernel's scratch buffers are laid out one after another, so each starts
    on the grain."""
    unit = hw.grain // int(np.gcd(hw.grain, 2 * width))
    return -(-rows // unit) * unit


def _emit(part, bufs, index, mask, hw: Hw) -> None:
    if isinstance(part, Fill):
        kt.fill_value(
            bufs[part.dst], 0, walk=((part.words, hw.word),), at=part.w0 * hw.word
        )
    elif isinstance(part, Words):
        kt.copy(
            bufs[part.dst],
            bufs[part.src],
            walk=part.levels,
            at=part.at,
            dwalk=part.dlevels,
            dat=part.dat,
        )
    elif isinstance(part, Splat):
        _emit_splat(part, bufs, mask, hw)
    elif isinstance(part, Shuffle):
        _emit_shuffle(part, bufs, mask, hw)
    elif isinstance(part, Pull):
        _emit_pull(part, bufs, index, mask, hw)
    elif isinstance(part, Exchange):
        TP.emit(
            bufs[part.src], mask, part.masks, bufs[part.dst], part.nb, part.A, part.B,
            hw.cores,
        )  # fmt: skip
    else:
        _emit_runs(part, bufs, index, mask, hw)


def _emit_splat(sp: Splat, bufs, mask, hw: Hw) -> None:
    """A trip: one word of values (a statistic of `lanes` rows) broadcast
    over that many rows of copies."""
    src, out, lanes = bufs[sp.src], bufs[sp.dst], hw.lanes
    ncore, start, n = V.split(sp.count, hw.cores)
    for c in kt.par(ncore, on=kt.vc):
        # A statistic broadcasts over GT4 bands; they store row-major.
        ones = kt.unpack(kt.load(mask[sp.ones : sp.ones + lanes, :], gt4=True))
        for k in kt.range(n(c)):
            w = start(c) + k
            stat = kt.unpack(kt.load(src[sp.w0 + w, :]))
            kt.store(out[w * lanes : w * lanes + lanes, :], kt.where(ones, stat, ones))


def _emit_pull(p: Pull, bufs, index, mask, hw: Hw) -> None:
    lanes, word = hw.lanes, hw.word
    src, dst, out = bufs[p.src], bufs[p.dst], bufs["po"]
    staged = p.rows * hw.row_words
    whole = ((p.words, word),)
    for k, how in enumerate(p.slots):
        ps = bufs[f"ps{k}"]
        if how[0] == "walk":
            kt.copy(ps, src, walk=how[1], at=how[2], dwalk=whole)
        else:
            pw = bufs[f"pw{k}"]
            kt.gather(pw[0 : p.words, :], src, _list(index, how[1], p.words), fp=True)
            kt.copy(ps, pw, walk=whole, dwalk=whole)
        if staged > p.words:
            kt.fill_value(ps, 0, walk=((staged - p.words, word),), at=p.words * word)
    if p.base == "dst":
        pd = bufs["pd"]
        kt.copy(pd, dst, walk=((p.out, word),), at=p.w0 * word, dwalk=((p.out, word),))
        if staged > p.out:
            kt.fill_value(pd, 0, walk=((staged - p.out, word),), at=p.out * word)
    ncore, start, n = V.split(p.rows // ROWS, hw.cores)
    for c in kt.par(ncore, on=kt.vc):
        for k in kt.range(n(c)):
            r = (start(c) + k) * ROWS
            if not p.tree:
                code = kt.unpack(kt.load(mask[p.codes + r : p.codes + r + ROWS, :]))
            acc = (
                kt.unpack(kt.load(bufs["pd"][r : r + ROWS, :]))
                if p.base == "dst"
                else 0.0
            )
            vals: dict = {}
            candidates = []
            for pair in p.pairs:
                t, rot = divmod(pair, lanes)
                if t not in vals:
                    vals[t] = kt.unpack(kt.load(bufs[f"ps{t}"][r : r + ROWS, :]))
                x = kt.xrot(vals[t], rot) if rot else vals[t]
                if p.tree:
                    candidates.append(x)
                else:
                    acc = kt.where(kt.math("cmpeq", code, float(pair)), x, acc)
            if p.tree:
                selectors, valid, zero = p.tree
                candidates += [0.0] * ((1 << len(selectors)) - len(candidates))
                for offset in selectors:
                    bit = kt.unpack(kt.load(mask[offset + r : offset + r + ROWS, :]))
                    candidates = [
                        kt.where(bit, candidates[i + 1], candidates[i])
                        for i in range(0, len(candidates), 2)
                    ]
                if valid >= 0:
                    mask_valid = kt.unpack(
                        kt.load(mask[valid + r : valid + r + ROWS, :])
                    )
                    acc = kt.where(mask_valid, candidates[0], acc)
                else:
                    acc = candidates[0]
                if zero >= 0:
                    mask_zero = kt.unpack(kt.load(mask[zero + r : zero + r + ROWS, :]))
                    acc = kt.where(mask_zero, 0.0, acc)
            elif p.zeros:
                acc = kt.where(kt.math("cmpeq", code, float(ZERO)), 0.0, acc)
            kt.store(out[r : r + ROWS, :], acc)
    kt.copy(dst, out, walk=((p.out, word),), dwalk=((p.out, word),), dat=p.w0 * word)


def _emit_shuffle(sh: Shuffle, bufs, mask, hw: Hw) -> None:
    lanes, B, word = hw.lanes, sh.B, hw.word
    g, rv = lanes // B, sh.nb * sh.Ap // hw.row
    s, y = bufs["sh"], bufs[sh.dst]
    for t in range(B):
        kt.copy(
            s,
            bufs[sh.src],
            walk=((sh.nb, sh.sb * 2), (sh.A // lanes, B * word)),
            at=(sh.at + t * lanes) * 2,
            dwalk=((sh.nb, sh.Ap * 2), (sh.A // lanes, word)),
            dat=t * rv * hw.row * 2,
        )
    ncore, start, count = V.split(rv // ROWS, hw.cores)
    for b, (m0, stages, taps) in enumerate(sh.progs):
        for c in kt.par(ncore, on=kt.vc):
            regs = [kt.unpack(kt.load(mask[m : m + 1, :])) for _, m in stages]
            tap = [kt.unpack(kt.load(mask[m : m + 1, :])) for m in taps]
            for k in kt.range(count(c)):
                r = (start(c) + k) * ROWS
                acc = None
                for t in range(B):
                    v = kt.unpack(kt.load(s[t * rv + r : t * rv + r + ROWS, :]))
                    for (kk, _), m in zip(stages, regs):
                        v = kt.where(m, kt.xrot(v, kk), v)
                    acc = (
                        v
                        if t == 0
                        else kt.where(tap[t - 1], kt.xrot(v, lanes - t * g), acc)
                    )
                kt.store(y[b * rv + r : b * rv + r + ROWS, :], acc)


def _emit_runs(r: Runs, bufs, index, mask, hw: Hw) -> None:
    lanes, dst, table = hw.lanes, bufs[r.dst], bufs[r.src]
    if r.margin:
        # The margin words are read only into lanes no segment selects.
        staged = bufs[margin_name(r)]
        kt.copy(
            staged,
            table,
            walk=((r.src_words, hw.word),),
            dwalk=((r.src_words, hw.word),),
            dat=r.margin * hw.word,
        )
        table = staged
    for g in r.groups:
        cols, rows = slice(0, g.words * lanes), slice(g.row0, g.row0 + g.rows)
        kt.gather(bufs["rs"][rows, cols], table, _list(index, g.at_s, g.rows), fp=True)
        if g.at_s2 is not None:
            kt.gather(
                bufs["rs2"][rows, cols], table, _list(index, g.at_s2, g.rows), fp=True
            )
    for g, lay in _in_order(r):
        cols = slice(0, g.words * lanes)
        dl = _list(index, lay.at_d, lay.rows)
        if lay.base == "dst":
            kt.gather(
                bufs["rd"][lay.row0 : lay.row0 + lay.rows, cols],
                dst,
                dl,
                fp=True,
                table_stride=(
                    hw.line * hw.word if dst.shape[-1] != hw.line_values else None
                ),
            )
        # Unrotated rows of every lane are their gathered words.
        copy = lay.classes[0].rows if _copies(lay.classes[0]) else 0
        if copy:
            rows = slice(lay.row0, lay.row0 + copy)
            kt.scatter(
                dst,
                bufs["rs"][rows, cols],
                _list(index, lay.at_d, copy),
                fp=True,
                table_stride=(
                    hw.line * hw.word if dst.shape[-1] != hw.line_values else None
                ),
            )
        if copy < lay.rows:
            for cl in lay.classes[1 if copy else 0 :]:
                _program(lay, cl, bufs, mask, hw)
            rows = slice(lay.row0 + copy, lay.row0 + lay.rows)
            # The copy rows' list is whole words of entries (a class is).
            at = lay.at_d + copy
            kt.scatter(
                dst,
                bufs["ro"][rows, cols],
                _list(index, at, lay.rows - copy),
                fp=True,
                table_stride=(
                    hw.line * hw.word if dst.shape[-1] != hw.line_values else None
                ),
            )


def _copies(cl: Cls) -> bool:
    return cl.rot == 0 and cl.mask < 0


def _list(index, at: int, n: int):
    """An index list's view: whole words of entries (`_Tables.put` pads)."""
    return index[at : at + -(-n // 8) * 8]


def _program(lay: Layer, cl: Cls, bufs, mask, hw: Hw) -> None:
    """One vector-core program (one stream loop): the class's rows rotated
    (lane l from lane l + r of the row's word, past the word from the next
    row's) and selected under its lane mask over the layer's base."""
    s, o, s2, d = bufs["rs"], bufs["ro"], bufs.get("rs2"), bufs.get("rd")
    per = cl.rows // ROWS // hw.cores
    for c in kt.par(hw.cores, on=kt.vc):
        rm = kt.unpack(kt.load(mask[cl.rmask : cl.rmask + 1, :])) if cl.rot else None
        em = (
            kt.unpack(kt.load(mask[cl.mask : cl.mask + 1, :])) if cl.mask >= 0 else None
        )
        for k in kt.range(per):
            r = cl.row0 + (c * per + k) * ROWS
            v = kt.unpack(kt.load(s[r : r + ROWS, :]))
            if cl.rot:
                v2 = kt.unpack(kt.load(s2[r : r + ROWS, :]))
                v = kt.where(rm, kt.xrot(v, cl.rot), kt.xrot(v2, cl.rot))
            if em is not None:
                base = (
                    kt.unpack(kt.load(d[r : r + ROWS, :])) if lay.base == "dst" else 0.0
                )
                v = kt.where(em, v, base)
            kt.store(o[r : r + ROWS, :], v)


__all__ = [
    "Cls",
    "Exchange",
    "Fill",
    "Group",
    "Hw",
    "Layer",
    "Plan",
    "Pull",
    "Runs",
    "Splat",
    "Unplanned",
    "Words",
    "canon",
    "execute",
    "hw_for",
    "hw_of",
    "layers",
    "plan",
    "positions",
    "relayout_kernel",
    "segments",
    "walk_words",
    "word_levels",
]
