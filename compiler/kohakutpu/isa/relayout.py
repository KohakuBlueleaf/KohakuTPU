"""A layout conversion as a vector-core program, instead of a host round trip.

THE GRANULARITY WALL, and why there are two kernels here. `vec_agu` walks
32-byte WORDS, so a conversion it can perform alone must be a permutation of
whole words. MEASURED, by packing an identity image through each layout's own
`pack`:

    tile:2x8:8x8 -> entry:8x2   over (64, 256)     0 of 1024 words survive
    flat         -> entry:8x2   over (4, 128, 64)  2048 of 2048 survive

A `Tile` word is a 4x4 sub-tile and the other two are sixteen consecutive
elements of ONE row, so they disagree BELOW the word and no stride splits one.
The disagreement is a 4x4 transpose of 8-byte granules inside each group of four
words, which :class:`Subtile` performs on the lanes. Bounded, as everything on
this core is, by four AGU dimensions, 256 words a walk, and `L1_SAFE`. See
`docs/projects/kohakutpu/relayout.md`.
"""

from dataclasses import dataclass, field

import numpy as np
from kohakuaccel.memory import Batched
from kohakutpu.hw import vector as V
from kohakutpu.hw.veckernels import L1_SAFE, S_VL, imem_flits, require_l1
from kohakutpu.isa.vecemit import AGU_DIMS, AGU_WALK, DIM_UNUSED, VecEmitError

from kohakutpu import layout as LO

#: fp16 elements in one 32-byte word, and in one 8-byte granule. A `Tile` word
#: is four granules that are four DIFFERENT rows; a `Flat` word is four that are
#: consecutive columns of one row. That is the whole disagreement.
WORD_ELEMS = LO.WORD_BYTES // 2
GRANULES = 4
GRAN_ELEMS = WORD_ELEMS // GRANULES

#: Instruction words :class:`Subtile` spends on one block, its preamble and its
#: tail, and what the core's instruction memory holds. A run that does not fit is
#: split, never truncated. 24 since `VSHUF` became predicable: four loads, four
#: rotates per output word, four stores, and no merge.
BLOCK_IMAGE = 24
PREAMBLE_WORDS = 28
IMEM_WORDS = 512

#: :class:`Strided` and :class:`Copy` are eight words whatever the run is: a
#: word permutation needs no lane pass, so the image does not scale.
PLAIN_IMAGE = 8

#: Words of L1 the lane-index table takes: one, holding 0,0,0,0,1,1,1,1,...
INDEX_WORDS = 1


def lane_groups() -> np.ndarray:
    """Which granule each of 16 lanes belongs to, as one fp16 word.

    Three `VCMPEQ` against it build the predicates a merge needs -- granule 0
    is the unconditional first write and needs none. It replaces the 0/1 mask
    table the `VSEL` merge used to read, and costs one L1 word instead of four.
    """
    return (np.arange(WORD_ELEMS) // GRAN_ELEMS).astype(np.float16)


def chunks_for(words: int) -> int:
    """Register chunks one :class:`Subtile` block covers: eight, or four.

    `VSHUF` rotates within each 16-lane chunk independently and every group of
    four words wants the SAME rotation, so one register at VL=128 carries eight
    groups and the same 36 instructions move 32 words. A run that is not whole
    32-word blocks falls back to VL=64 rather than to the host.
    """
    return 2 * GRANULES if not words % (2 * GRANULES * GRANULES) else GRANULES


# --------------------------------------------------------------- the orders
def _identity(shape: tuple) -> tuple:
    """Two fp16 images whose bit patterns number every element from one.

    Sixteen bits cannot number a buffer past 65,535 elements, so the index is
    SPLIT across two images and recombined; the layouts only move bytes, so both
    halves come back in the same place and the pair identifies an element
    exactly. Numbering from ONE keeps a real element clear of a zero the padding
    wrote.
    """
    n = int(np.prod(shape))
    idx = np.arange(1, n + 1, dtype=np.uint32)
    lo = (idx & 0xFFFF).astype(np.uint16).view(np.float16).reshape(shape)
    hi = (idx >> 16).astype(np.uint16).view(np.float16).reshape(shape)
    return lo, hi


def image(layout, shape: tuple) -> np.ndarray:
    """`layout`'s byte order over `shape`, as one uint32 identity per element.

    Read off the LAYOUT'S OWN `pack`, which is held by SHA in
    `tests/test_layout.py`, so nothing here is a second statement of the orders.
    Returns a `(words, 16)` array, or None when the order pads.
    """
    lo, hi = _identity(shape)
    a = np.frombuffer(layout.pack(lo), np.uint16).astype(np.uint32)
    b = np.frombuffer(layout.pack(hi), np.uint16).astype(np.uint32)
    if a.size != b.size or a.size % WORD_ELEMS:
        return None
    return (a | (b << 16)).reshape(-1, WORD_ELEMS)


def granule_transpose(img: np.ndarray) -> np.ndarray | None:
    """`img` with the 4x4 array of granules transposed inside each group of four.

    None when the image is not whole groups of four words, which cannot be
    transposed without splitting a group across two runs.
    """
    if img.shape[0] % GRANULES:
        return None
    g = img.reshape(-1, GRANULES, GRANULES, GRAN_ELEMS)
    return g.transpose(0, 2, 1, 3).reshape(-1, WORD_ELEMS)


def word_perm(src: np.ndarray, dst: np.ndarray):
    """`dst` word index for each `src` word, or None if `dst` is not a permutation.

    Refuses a repeated source word rather than picking one: two identical words
    mean the identity did not identify, and choosing would silently place the
    wrong one.
    """
    if src.shape != dst.shape:
        return None
    have: dict = {}
    for i, w in enumerate(src):
        key = w.tobytes()
        if key in have:
            return None
        have[key] = i
    out = np.empty(len(dst), np.int64)
    for i, w in enumerate(dst):
        at = have.get(w.tobytes())
        if at is None:
            return None
        out[at] = i
    return out


def affine_dims(walk: np.ndarray, limit: int = AGU_DIMS) -> list | None:
    """`walk` as `(stride, bound)` dims innermost first, or None.

    A permutation the AGU can walk is a mixed-radix digit rearrangement, so each
    dimension is the step that repeats at the span below it. Anything else --
    and anything past `limit` dimensions -- is not a walk `vec_agu` has, and
    returning None sends the caller back to a host relayout, which is slow and
    correct where a truncated walk would be fast and wrong.
    """
    n = len(walk)
    if n == 1:
        return []
    dims: list = []
    span = 1
    while span < n:
        step = int(walk[span]) - int(walk[0])
        bound = 1
        while span * (bound + 1) <= n:
            block = walk[span * bound : span * (bound + 1)] - walk[:span]
            if not np.all(block == bound * step):
                break
            bound += 1
        if bound == 1:
            return None
        dims.append((step, bound))
        span *= bound
    if span != n or len(dims) > limit:
        return None
    return dims


# ----------------------------------------------------------------- the plan
@dataclass
class Relayout:
    """One conversion as the passes a vector core runs.

    `mode` says which side of the pass carries the permutation: ``drain`` fills
    a contiguous run and writes it scattered, ``fill`` gathers and writes a
    contiguous run. `transpose` asks for the lane pass, which is needed exactly
    when a `Tile` order is on one side.
    """

    words: int
    dims: list
    mode: str
    transpose: bool
    src_off: list = field(default_factory=list)
    dst_off: list = field(default_factory=list)
    total: int = 0
    #: Destination word for each source word. Kept because the SHARD question --
    #: does the layout change move a word out of its own block -- is answered
    #: from it exactly, with no search.
    perm: object = None

    @property
    def runs(self) -> int:
        return len(self.src_off)

    @property
    def nbytes(self) -> int:
        return self.total * LO.WORD_BYTES

    @property
    def stepped(self) -> bool:
        """Whether the runs step contiguously on BOTH sides."""
        return self.src_off == self.dst_off

    @property
    def inplace(self) -> bool:
        """Whether this may be walked over the buffer itself, with no staging.

        A permutation is not in general performable in place -- a run writes
        words a later run has still to read. It IS when every run permutes only
        its OWN words: the fill has brought all of them into L1 behind a VBAR
        before the drain starts, so the source they came from is free.
        """
        return self.stepped and sorted(_offsets(self.dims)) == list(range(self.words))

    def summary(self) -> str:
        kind = "subtile" if self.transpose else "strided"
        return (
            f"{kind}/{self.mode} {self.total} words in {self.runs} runs of "
            f"{self.words}, dims {self.dims}"
        )


def shard_blocks(made: Relayout, shards: int):
    """Source and destination shard of every word, when the buffer splits `shards` ways.

    `docs/notes/data-movement-problem.md` §7 question 3 -- "formalise the shard
    axis survives the layout change" -- answered exactly rather than by search:
    a sharding is a partition of the buffer into `shards` contiguous blocks, and
    a layout change preserves it iff no word leaves its own block.

    None when the buffer does not divide, since a shard is contiguous blocks and
    a ragged split is a different question.
    """
    if made.perm is None or made.total % shards:
        return None
    span = made.total // shards
    here = np.arange(made.total) // span
    return here, np.asarray(made.perm) // span


def shard_local(made: Relayout, shards: int) -> bool:
    """Whether this layout change needs NO cross-unit traffic at all.

    The single highest-value compile-time test in §7: it predicts ZERO link
    credits, which are the most expensive ones there are.
    """
    got = shard_blocks(made, shards)
    return got is not None and bool(np.array_equal(got[0], got[1]))


def crossing(made: Relayout, shards: int) -> int:
    """Bytes that change unit under this layout change, hop-weighted.

    Hops on an OPEN PATH, so `|u - v|` in path position -- a machine whose mesh
    indices are not path order passes them through `MachineSpec.mesh_hops`
    first. Zero exactly when :func:`shard_local`.
    """
    got = shard_blocks(made, shards)
    if got is None:
        return 0
    here, there = got
    return int(np.abs(there - here).sum()) * LO.WORD_BYTES


def _offsets(dims: list) -> list:
    """Every word offset this walk visits, dimension 0 fastest."""
    total = 1
    for _, bound in dims:
        total *= bound
    out = []
    for step in range(total):
        rest, at = step, 0
        for stride, bound in dims:
            at += (rest % bound) * stride
            rest //= bound
        out.append(at)
    return out


def _fits(dims: list) -> bool:
    """Whether every stride this walk needs fits `vec_agu`'s signed 18 bits."""
    span = 1 << 17
    return all(
        -span <= stride * LO.WORD_BYTES < span and 0 < bound <= 0xFFFF
        for stride, bound in dims
    )


def cost(words: int, total: int, batch: int, transpose: bool) -> int:
    """Flits one conversion costs at this run width.

    The image is loaded ONCE for a whole batch and every run restates two bases
    and a RUN, so a wider run trades instruction words against run flits. There
    is a minimum in between and it is not the widest run -- MEASURED, a 1,024
    word transpose over four batch elements is 317 flits at 128 words a run and
    413 at 256.
    """
    runs = -(-total // words)
    if not transpose:
        return PLAIN_IMAGE + _PLAIN_FIXED + 3 * runs * batch
    blocks = -(-words // (GRANULES * chunks_for(words)))
    image = PREAMBLE_WORDS + BLOCK_IMAGE * blocks
    return image + _SUBTILE_FIXED + 3 * runs * batch


#: Descriptor and reset flits each kernel spends once per program: its dims
#: stated, then returned. Two descriptors for a plain walk, five for a subtile.
_PLAIN_FIXED = 2 * (2 * AGU_DIMS)
_SUBTILE_FIXED = 2 + 1 + 2 * (5 * AGU_DIMS)


def _split(perm: np.ndarray, grain: int, cap: int, total: int, batch: int, tr: bool):
    """The best RUN width, as `(words, dims)`.

    A RUN fills one contiguous run and drains one walk, so the walk RELATIVE to
    each run's own start has to be the same walk -- otherwise the descriptor
    would have to be rewritten inside the program, which no RUN does.

    IN PLACE FIRST, cheapest second. A run width whose walk stays inside its own
    span needs no staging span and no second pass over the buffer, which is two
    DRAM touches of the whole tensor -- always worth more than the handful of
    flits a different width would save.
    """
    n = len(perm)
    best = None
    for words in range(min(cap, n) // grain * grain, 0, -grain):
        if n % words:
            continue
        rel = perm[:words] - perm[0]
        starts = perm[::words]
        if not np.array_equal(
            perm.reshape(-1, words) - starts[:, None], np.tile(rel, (len(starts), 1))
        ):
            continue
        dims = affine_dims(rel)
        if dims is None or not _fits(dims):
            continue
        held = np.array_equal(starts, np.arange(len(starts)) * words) and sorted(
            _offsets(dims)
        ) == list(range(words))
        score = (0 if held else 1, cost(words, total, batch, tr))
        if best is None or score < best[0]:
            best = (score, words, dims)
    return None if best is None else (best[1], best[2])


def _cap(transpose: bool) -> tuple:
    """`(grain, words)` a RUN of this kernel may take.

    A plain run holds only the filled words; a transposing one writes its output
    OVER them and holds the mask table beside it, so both are bounded by
    `L1_SAFE` well above the 256-word walk `vec_core` faults past.
    """
    if not transpose:
        return 8, min(AGU_WALK, L1_SAFE)
    fits = (IMEM_WORDS - PREAMBLE_WORDS - 2) // BLOCK_IMAGE * (2 * GRANULES * GRANULES)
    room = L1_SAFE - INDEX_WORDS
    grain = GRANULES * GRANULES
    return grain, min(AGU_WALK, room, fits) // grain * grain


def plan(before, after, shape: tuple, batch: int = 1) -> Relayout | None:
    """How a vector core would rewrite `shape` from `before` into `after`.

    `batch` is how many buffers this conversion covers, which only changes which
    RUN width is cheapest -- see :func:`cost`.

    None for a conversion this machine cannot perform: the two orders pad
    differently, or the permutation needs more than four dimensions at every run
    width. The caller then relayouts through the host, which is what it did
    before this existed.
    """
    src, dst = image(before, shape), image(after, shape)
    if src is None or dst is None or src.shape != dst.shape:
        return None
    for transpose in (False, True):
        held = granule_transpose(src) if transpose else src
        away = granule_transpose(dst) if transpose else dst
        if held is None or away is None:
            continue
        grain, cap = _cap(transpose)
        got = (grain, cap, len(src), batch, transpose)
        # DRAIN side: L1 holds the source's own order, so a transpose there acts
        # on groups of consecutive SOURCE words.
        made = _plan_one(word_perm(held, dst), "drain", transpose, *got)
        if made is not None:
            return made
        # FILL side: the gather puts L1 in the DESTINATION's order, so the
        # transpose acts on groups of consecutive destination words instead.
        back = word_perm(src, away)
        if back is None:
            continue
        made = _plan_one(_invert(back), "fill", transpose, *got)
        if made is not None:
            return made
    return None


def _invert(perm: np.ndarray) -> np.ndarray:
    out = np.empty_like(perm)
    out[perm] = np.arange(len(perm))
    return out


def _plan_one(perm, mode: str, transpose: bool, grain, cap, total, batch, tr):
    """One direction's plan, or None when its walk does not fit the core."""
    if perm is None:
        return None
    got = _split(perm, grain, cap, total, batch, tr)
    if got is None:
        return None
    words, dims = got
    walked = [int(perm[r * words]) for r in range(total // words)]
    flat = [r * words for r in range(total // words)]
    src, into = (flat, walked) if mode == "drain" else (walked, flat)
    return Relayout(words, dims, mode, transpose, src, into, total, perm)


# --------------------------------------------------------------- the kernels
class Strided:
    """A word permutation: fill one side contiguously, walk the other.

    NO LANE PASS AND NO L1 COPY. A `VFILL` lands its walk contiguously in L1 from
    `l1off` and a `VDRAIN` reads contiguously from `l1off` onto its own walk, so
    putting the permutation on one of the two descriptors performs it outright:
    the image is eight words whatever the run width, against 70 for the identity
    chain `ElementwiseKernel(drain=)` needs to move the same bytes through the
    lanes.
    """

    AD_FILL, AD_DRAIN = 0, 1

    def __init__(self, made: Relayout) -> None:
        if made.transpose:
            raise VecEmitError("a subtile relayout needs the lane pass, not this one")
        self.made = made
        self.w = made.words
        require_l1(f"relayout {self.w}w", self.w)
        self.image = [
            V.vseti(S_VL),
            V.VLMAX,
            V.vsetvl(S_VL),
            V.vsetmode(V.FLAT),
            V.vfill(self.AD_FILL, 0),
            V.vbar(),
            V.vdrain(self.AD_DRAIN, 0),
            V.vhalt(),
        ]
        _sized(self.image)

    def static_descs(self) -> list[int]:
        """Whichever of fill and drain carries the walk; the other is one run."""
        return _walk_descs(self.made, self.AD_FILL, self.AD_DRAIN, self.w)

    def flits(self, src: int, dst: int, mask: int = 0) -> list[int]:
        return self.program([(src, dst)])

    def program(self, pairs, mask: int = 0) -> list[int]:
        """One image for EVERY buffer in `pairs`, which is a batch's worth.

        The image and the dimension fields do not depend on the base, so a
        batched conversion loads them once and restates two bases per run --
        against re-sending a whole image per batch element.
        """
        out = imem_flits(self.image) + self.static_descs()
        for src, dst in pairs:
            out += _runs(self.made, src, dst)
        return out + reset_descs((self.AD_FILL, self.AD_DRAIN))


class Subtile:
    """The 4x4 granule transpose on the lanes, plus the word walk on the AGU.

    Four source words go into four registers and come out as four destination
    words: output `i` takes granule `i` of input `j` and puts it at granule `j`,
    which is a rotation of `4(i-j)` lanes. `VSHUF` rotates WITHIN each 16-lane
    chunk and every group wants the same rotation, so one register at VL=128
    carries EIGHT groups and 24 instructions move 32 words.

    The rotate is PREDICATED, so the four writes land in their own granule's
    lanes and there is no merge at all -- 12 `VSEL` per block gone. It computes
    nothing either way, so a value crosses this pass bit for bit. The output is
    written back OVER the input in L1 -- a block's four loads all precede its
    four stores -- which is what lets one descriptor serve both.
    """

    AD_FILL, AD_DRAIN, AD_LDST, AD_MFILL, AD_MLD = 0, 1, 2, 3, 4
    TOUCHED = (AD_FILL, AD_DRAIN, AD_LDST, AD_MFILL, AD_MLD)

    #: Registers: four filled words, the output, and the lane-group index the
    #: predicates are built from. No merge register: the rotate writes in place.
    R_IN, R_ACC, R_IDX = (0, 1, 2, 3), 4, 5
    #: Scalars: four rotation amounts then three granule numbers to compare
    #: against. S0 is VL and S1..S3 are VRED's.
    S_ROT, S_GROUP = 4, 8

    def __init__(self, made: Relayout) -> None:
        if not made.transpose:
            raise VecEmitError("a plain word permutation does not need the lane pass")
        self.made = made
        self.w = made.words
        self.chunks = chunks_for(self.w)
        self.block = GRANULES * self.chunks
        if self.w % self.block:
            raise VecEmitError(
                f"a {self.w}-word run is not whole {self.block}-word blocks; a "
                f"group of four words split across two RUNs is half a transpose"
            )
        self.vl = self.chunks * WORD_ELEMS
        self.index_word = self.w
        require_l1(f"subtile relayout {self.w}w", self.w + INDEX_WORDS)

        pre = [V.vseti(S_VL), self.vl]
        for n in range(GRANULES):
            pre += [V.vseti(self.S_ROT + n), n * GRAN_ELEMS]
        for j in range(1, GRANULES):
            pre += [V.vseti(self.S_GROUP + j - 1), V.e8m15(float(j))]
        pre += [
            V.vsetvl(S_VL),
            V.vsetmode(V.FLAT),
            V.vfill(self.AD_MFILL, self.index_word),
            V.vfill(self.AD_FILL, 0),
            V.vbar(),
            # Stride 0: the one index word reaches every chunk of the register,
            # so the predicates repeat per 16 lanes exactly as the rotate does.
            V.vld(self.R_IDX, self.AD_MLD, 0),
        ]
        pre += [
            V.alu(
                "VCMPEQ",
                vd=0,
                va=self.R_IDX,
                vb=self.S_GROUP + j - 1,
                sb=V.SRC_S,
                pr=j,
            )
            for j in range(1, GRANULES)
        ]

        body: list[int] = []
        for at in range(0, self.w, self.block):
            for j in range(GRANULES):
                body.append(V.vld(self.R_IN[j], self.AD_LDST, at + j))
            for i in range(GRANULES):
                body.append(self._rot(self.R_ACC, self.R_IN[0], i, 0))
                for j in range(1, GRANULES):
                    body.append(self._rot(self.R_ACC, self.R_IN[j], i, j, pr=j))
                body.append(V.vst(self.R_ACC, self.AD_LDST, at + i))
        self.image = pre + body + [V.vdrain(self.AD_DRAIN, 0), V.vhalt()]
        _sized(self.image)

    def _rot(self, dst: int, src: int, i: int, j: int, pr: int = 0) -> int:
        """`dst = rot(src, 4(i-j))`, which lands input `j`'s granule `i` at `j`.

        `vd[l] = va[(l + k) % 16]`, so granule `i` reaches granule `j` when
        `4j + k == 4i`; the amount is taken modulo the chunk, never negative.

        `pr` predicates the WRITE to granule `j`'s own four lanes, which is what
        replaced a merge: granule 0 is the unconditional first write and the
        other three land on top of it, each in its own lanes.
        """
        return V.alu(
            "VSHUF",
            vd=dst,
            va=src,
            vb=self.S_ROT + ((i - j) % GRANULES),
            pr=pr,
            pm=1 if pr else 0,
        )

    def static_descs(self) -> list[int]:
        """The L1 window, the mask read, and whichever side carries the walk.

        The window steps FOUR words: register `j` takes word `j` of each of
        `chunks` consecutive groups, which is where the lane width comes from.
        """
        out = [
            V.desc_flit(self.AD_LDST, 0, 0),
            V.desc_flit(self.AD_MLD, 0, self.index_word),
            *_dims(self.AD_LDST, [(GRANULES, self.chunks)]),
            *_dims(self.AD_MFILL, [(V.WORD_BYTES, INDEX_WORDS)]),
            # Stride 0: one word read into every chunk of the register.
            *_dims(self.AD_MLD, [(0, self.chunks)]),
        ]
        return out + _walk_descs(self.made, self.AD_FILL, self.AD_DRAIN, self.w)

    def flits(self, src: int, dst: int, mask: int = 0) -> list[int]:
        return self.program([(src, dst)], mask)

    def program(self, pairs, mask: int = 0) -> list[int]:
        """One image and one mask fill for every buffer in `pairs`."""
        out = imem_flits(self.image) + self.static_descs()
        out.append(V.desc_flit(self.AD_MFILL, 0, mask))
        for src, dst in pairs:
            out += _runs(self.made, src, dst)
        return out + reset_descs(self.TOUCHED)


class Copy:
    """A contiguous word copy, which is what returns a converted buffer home.

    A conversion is IN PLACE from the caller's side, and a permutation that
    moves words BETWEEN runs cannot be performed in place -- a run would write
    words a later run has still to read. So the walk lands in a staging span and
    this brings it back.
    """

    AD_FILL, AD_DRAIN = 0, 1

    def __init__(self, words: int) -> None:
        cap = min(AGU_WALK, L1_SAFE)
        self.w = max((w for w in range(cap, 0, -1) if not words % w), default=0)
        if not self.w:
            raise VecEmitError(f"a {words}-word copy has no run width under {cap}")
        self.runs = words // self.w
        require_l1(f"relayout copy {self.w}w", self.w)
        self.image = [
            V.vseti(S_VL),
            V.VLMAX,
            V.vsetvl(S_VL),
            V.vsetmode(V.FLAT),
            V.vfill(self.AD_FILL, 0),
            V.vbar(),
            V.vdrain(self.AD_DRAIN, 0),
            V.vhalt(),
        ]
        _sized(self.image)

    def flits(self, src: int, dst: int) -> list[int]:
        return self.program([(src, dst)])

    def program(self, pairs) -> list[int]:
        """One image for every buffer in `pairs`; only the two bases move."""
        out = imem_flits(self.image) + [
            *_dims(self.AD_FILL, [(V.WORD_BYTES, self.w)]),
            *_dims(self.AD_DRAIN, [(V.WORD_BYTES, self.w)]),
        ]
        step = self.w * LO.WORD_BYTES
        for src, dst in pairs:
            for r in range(self.runs):
                out += [
                    V.desc_flit(self.AD_FILL, 0, src + r * step),
                    V.desc_flit(self.AD_DRAIN, 0, dst + r * step),
                    V.run_flit(0),
                ]
        return out + reset_descs((self.AD_FILL, self.AD_DRAIN))


def build(made: Relayout):
    """The kernel object for this plan."""
    return Subtile(made) if made.transpose else Strided(made)


def for_conversion(before, after, shape: tuple):
    """``(plan, src stride, dst stride, count)`` for a conversion, or None.

    A batched buffer is `count` independent blocks at one stride, so it is
    planned ONCE for the block and run per element -- the padding between
    elements belongs to neither order and nothing here touches it.

    The two strides may DIFFER, which is what an out-of-place reorder between
    two separately allocated buffers needs; an in-place caller checks that they
    agree, since it has only one buffer to step.
    """
    held, into = before, after
    src, dst, count, block = 0, 0, 1, tuple(shape)
    if isinstance(before, Batched) or isinstance(after, Batched):
        if not (isinstance(before, Batched) and isinstance(after, Batched)):
            return None
        if (before.count, before.block) != (after.count, after.block):
            return None
        held, into = before.inner, after.inner
        src, dst = before.stride, after.stride
        count, block = before.count, tuple(before.block)
    made = plan(held, into, block, count)
    if made is None:
        return None
    try:
        build(made)
        if not made.inplace:
            Copy(made.total)
    except (VecEmitError, ValueError):
        return None
    return made, src, dst, count


def image_cycles(words: list, vl: int) -> int:
    """Cycles one RUN of this image costs, by `hw.vector.cycles`.

    `VSETI` carries its immediate in the NEXT word, which the core fetches and
    does not execute -- charging it would price every constant twice.
    """
    out, at = 0, 0
    while at < len(words):
        op = (int(words[at]) >> 27) & 0x1F
        out += V.cycles(op, vl)
        at += 2 if op == V.OPS["VSETI"] else 1
    return out


def cycles_for(made: Relayout, count: int = 1) -> int:
    """Cycles a vector core spends performing this conversion `count` times.

    Every flit costs one cycle to take and a RUN costs what its image ran, which
    is what `model.VectorUnit.cost` charges -- so the instruction stream is IN
    this figure and not beside it.

    The staged form is TWO passes over the buffer, the walk into staging and the
    copy home, which is what makes an in-place plan worth preferring over a
    cheaper walk.
    """
    kernel = build(made)
    pairs = [(0, 0)] * count
    out = image_cycles(kernel.image, getattr(kernel, "vl", V.VLMAX)) * made.runs * count
    out += len(kernel.program(pairs)) - made.runs * count
    if not made.inplace:
        back = Copy(made.total)
        out += image_cycles(back.image, V.VLMAX) * back.runs * count
        out += len(back.program(pairs)) - back.runs * count
    return out


def reset_descs(ads) -> list[int]:
    """`ads`' dimensions back to `vec_agu`'s reset state.

    A descriptor is CORE STATE and outlives the program that wrote it, so a
    relayout has to leave the file as it found it -- the kernel that runs next
    states only the dimensions IT uses and inherits the rest. Measured: a
    following band's VFILL walked 2,048 entries and faulted F_LEN.

    Only the descriptors this program WROTE need it: every kernel here states
    all four dimensions of each one it uses, so it inherits nothing and dirties
    nothing else.
    """
    return [
        V.desc_flit(ad, n + 1, V.dim(*DIM_UNUSED))
        for ad in ads
        for n in range(AGU_DIMS)
    ]


def _dims(ad: int, dims: list, scale: int = 1) -> list:
    """Descriptor `ad`'s four dimension fields, unused ones RETURNED TO RESET."""
    out = []
    for n in range(AGU_DIMS):
        stride, bound = dims[n] if n < len(dims) else DIM_UNUSED
        out.append(V.desc_flit(ad, n + 1, V.dim(stride * scale, bound)))
    return out


def _sized(words: list) -> None:
    if len(words) > IMEM_WORDS:
        raise VecEmitError(
            f"a {len(words)}-word relayout image is over the {IMEM_WORDS} "
            f"instruction words a core holds; take a narrower run"
        )


def _walk_descs(made: Relayout, ad_fill: int, ad_drain: int, words: int) -> list:
    """The dims for the two transfers: one contiguous, one the plan's walk."""
    ad = ad_drain if made.mode == "drain" else ad_fill
    other = ad_fill if made.mode == "drain" else ad_drain
    return _dims(other, [(V.WORD_BYTES, words)]) + _dims(ad, made.dims, V.WORD_BYTES)


def _runs(made: Relayout, src: int, dst: int) -> list:
    """One RUN per run of the plan, each with its own two bases.

    Stated per run rather than stepped: the outer dimensions of a conversion are
    not one arithmetic progression -- a grid axis and a K axis interleave -- and
    a uniform step would put half the runs somewhere else.
    """
    out: list[int] = []
    for r in range(made.runs):
        out.append(V.desc_flit(0, 0, src + made.src_off[r] * LO.WORD_BYTES))
        out.append(V.desc_flit(1, 0, dst + made.dst_off[r] * LO.WORD_BYTES))
        out.append(V.run_flit(0))
    return out
