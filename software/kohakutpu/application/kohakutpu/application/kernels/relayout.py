"""Relayouts of f16 elements on the card, exact, as one kernel of the
cheapest parts the card has (`forms.relayout`).

The walk pair is a set of axes ``(count, source stride, destination
stride)`` in order of destination stride, adjacent axes both walks step as
one merged; the innermost destination axis picks the form:

- `Words`: both walks whole words: one mover walk.
- runs (`Runs`): the axis contiguous in the source too. Runs are cut into
  segments inside one vector-core row of destination words; a segment's
  source words are one gathered row (and the next word's row when it is
  rotated: the gathered row a word wider, walked one word on, where it fits
  a vector-core row), rotated and masked in the vector core, and scattered back as
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

Other walk pairs are unplanned (the lanes plan takes them). The index lists
and masks are data; the kernel depends on the plan's shape only.

The destination port is the destination's words from its line: a storage
holds its word-rounded bytes, API.MEM.002. Where they end inside a line and
runs write the destination, the parts write the lines of a scratch and one
walk copies its words out.

The planner is `ktpu_kernels::gen::relayout`; this module holds the plan
parts and their DSL statements.
"""

import json
from dataclasses import dataclass

import numpy as np

from kohakutpu.application.kernels import transpose as TP
from kohakutpu.application.kernels import vector as V
from kohakutpu.language import dsl as kt
from kohakutpu.language import machine as MF
from kohakutpu.stack.rs import _rs

#: Vector-core rows a trip takes.
ROWS = 2
_DEFAULTS = json.loads(_rs.RELAYOUT_DEFAULTS)
#: Words one move takes (`ktpu_kernels::gen::relayout::MOVE_WORDS`, ISA.MV.007).
MOVE_WORDS = _DEFAULTS["move_words"]
#: Whether a `Pull`'s codes are predecoded into a selector tree (`tree_pull`)
#: unless `KTPU_PULL_TREE` says; the default is the native planner's
#: (`ktpu_kernels::gen::relayout::PULL_TREE`).
PULL_TREE = _DEFAULTS["pull_tree"]


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


def _in_order(r: Runs) -> list:
    """``(group, layer)`` in the order they run: by layer, then width."""
    return sorted(
        ((g, lay) for g in r.groups for lay in g.layers), key=lambda gl: gl[1].k
    )


def margin_name(r: Runs) -> str:
    """The staged copy of a table with words of 0 around it: one buffer a
    table, its copy writing part of its first and last lines (L1.VER.017:
    no other move may)."""
    return f"rm_{r.src}"


def emit_steps(plans: tuple, src, tables: dict, prefix: str, hw: Hw, destination):
    """Statements of `plans` inside the kernel being traced, the last into
    `destination`, each before it into a scratch of lines: the first reading
    `src` (a word table), each next the one before (its lines copied into a
    word table); `tables` maps ``{prefix}index{i}`` / ``{prefix}mask{i}`` to
    their ports. Every buffer on the grain (L2.REL.013). Returns
    `destination`."""
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
            if i == len(plans) - 1
            else scratch(-(-p.dst_words // hw.line), hw.line_values)
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
        # The padding from the line the words end in first: then the copy is
        # the one move writing part of a line (L1.VER.017).
        pd, head = bufs["pd"], p.out - p.out % hw.line
        if staged > head:
            kt.fill_value(pd, 0, walk=((staged - head, word),), at=head * word)
        kt.copy(pd, dst, walk=((p.out, word),), at=p.w0 * word, dwalk=((p.out, word),))
    ncore, start, n = V.split(p.rows // ROWS, hw.cores)
    for c in kt.par(ncore, on=kt.vc):
        for k in kt.range(n(c)):
            r = (start(c) + k) * ROWS
            code = 0.0
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
                acc = 0.0
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
        # A rotated group's next words: one word more a gathered row, then the rows
        # walked one word on, where that word fits the row.
        shifted = g.at_s2 is not None and g.words < hw.row_words
        cols, rows = slice(0, g.words * lanes), slice(g.row0, g.row0 + g.rows)
        wide = slice(0, (g.words + shifted * hw.line) * lanes)
        kt.gather(bufs["rs"][rows, wide], table, _list(index, g.at_s, g.rows), fp=True)
        if shifted:
            _next_words(g, bufs["rs"], bufs["rs2"], hw)
        elif g.at_s2 is not None:
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


def _next_words(g, rs, rs2, hw: Hw) -> None:
    """Group `g`'s rows of `rs2` := its rows of `rs` one word on, as one
    contiguous run (a run costs the mover what an indexed row does), in moves
    of at most `MOVE_WORDS` words. Whole rows are whole lines (L1.VER.017);
    the last word, from the row past the group's, is no word a segment reads."""
    start = g.row0 * hw.row * 2
    words = g.rows * hw.row_words
    for done in range(0, words, MOVE_WORDS):
        walk = ((min(MOVE_WORDS, words - done), hw.word),)
        at = start + done * hw.word
        kt.copy(rs2, rs, walk=walk, at=at + hw.word, dwalk=walk, dat=at)


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
        rm = kt.unpack(kt.load(mask[cl.rmask : cl.rmask + 1, :])) if cl.rot else 0.0
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
    "Hw",
    "Layer",
    "Pull",
    "Runs",
    "Splat",
    "Words",
    "hw_for",
    "hw_of",
]
