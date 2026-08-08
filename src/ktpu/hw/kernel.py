"""Tiling: turning any GEMM into work the machine can actually hold.

The hardware holds ONE tile of the problem at a time:

    gm * gn <= TILES        output sub-tiles resident in the accumulator
    gm * nk <= L1_ENTRIES   A's L1 entries
    gn * nk <= L1_ENTRIES   B's

Anything larger is a loop over that tile, and this module is that loop.

THREE DECISIONS SHAPE EVERYTHING HERE.

**K is innermost.** The accumulator keeps an output tile resident across the
whole K sweep and writes it to memory once. Any other order spills a partial
tile per K chunk and reads it back, costing `M*N*(K/Kc)` of write traffic
instead of `M*N` -- 32x at K=4096, plus the same again in reads. `GEMM.acc` is
what makes the good order expressible.

**Operands are stored tile-major.** A pass needs the entries for one (tile,
K-chunk); in the natural group-major order those are gm separate runs, so the
pass would need gm FILL instructions or the hardware would need a strided
fetch. Reordering memory removes the problem for free -- see
`tensor.to_fp16_words_tiled`.

**The program is control; the flits are data.** A pass is `3*chunks+1`
instruction flits uploaded straight into the staging RAM. The program only
names a destination and kicks, so it costs ~6 commands per pass instead of
~20 words of instruction.

WHEN IT STILL DOES NOT FIT, IT STREAMS. A large GEMM has more passes than the
staging window, the command RAM and the dispatch credit allow at once, so
passes are cut into ROUNDS -- each a self-contained upload / load / GO -- and
the machine runs them back to back. The card never needs the whole program, and
nothing about the result depends on where the cuts fall.
"""

import itertools
from dataclasses import dataclass, field

from ktpu.hw import device as dev
from ktpu.hw import matmul, tensor

WB = 32  # bytes per memory word

# Entries one FILL instruction may name. The memory request's streaming `count`
# is 8 bits (docs/isa/memory.md s2), and 256 wraps to 0, which memory coerces
# to 1 -- so the CU waits forever for 255 entries nobody asked for, the whole
# result is zeros, and nothing reports anything.
#
# It does not bound the CHUNK, only the instruction: `eoff` says where a fill
# lands, so a longer run is two fills at consecutive offsets. That costs one
# extra descriptor round trip (~29 cycles) and no protocol change at all --
# which is why the field stays 8 bits and `choose_tile` does not cap `nk`.
FILL_MAX_ENTRIES = 255


@dataclass(frozen=True)
class TileShape:
    """One pass's worth of work, in sub-tile units."""

    gm: int  # output row groups, 4 rows each
    gn: int  # output col groups, 4 cols each
    nk: int  # K blocks per chunk, 32 elements each

    @property
    def m(self):
        return self.gm * tensor.LANES

    @property
    def n(self):
        return self.gn * tensor.LANES

    @property
    def k(self):
        return self.nk * tensor.KBLOCK

    def __str__(self):
        # M x K x N, like every other shape in the driver.
        return f"{self.m}x{self.k}x{self.n}"


def choose_tile(tiles, l1_a, k_blocks, l1_b=None, m=None, k=None, n=None, banks=2):
    """The pass with the most reuse the hardware can hold.

    Ranked by ARITHMETIC INTENSITY first, `2*gm*gn/(gm+gn)` MACs per operand
    byte, because that is the quantity every other cost divides into: it is
    what decides how many bytes the fetch path must move per unit of compute,
    and no amount of scheduling moves it. Then K per fill, since operands are
    re-read every pass and only the output stays put; then squareness, to break
    ties toward the shape with the smaller padding bill.

    Maximising `gm*gn` instead -- which is what this did -- is not the same
    thing. 32x1 and 8x4 both hold 32 sub-tiles; the first has intensity 1.94
    and the second 5.33. Residency is the CONSTRAINT, not the objective.
    """
    l1_b = l1_a if l1_b is None else l1_b
    best = None
    # POWERS OF TWO ONLY, and this is a real constraint rather than tidiness.
    # Ranked on intensity alone the best tile at 512 sub-tiles is 22x23 -- 22.5
    # MACs/byte, marginally above 16x32's 21.3 -- and its output block is 88x92,
    # so every dimension of every problem pads up to a multiple of 88 or 92. A
    # 1024-cube pads to 1056x1104 and pays 11% before any efficiency is
    # counted. A power-of-two block pads it by nothing.
    #
    # The intensity difference is 5%; the padding difference is not, and it
    # falls on exactly the ragged shapes a large tile is supposed to be safe
    # for. This is the guard that keeps "bigger tile" from quietly becoming a
    # regression on real problems.
    sizes = [1 << e for e in range(12) if (1 << e) <= tiles]
    for gm in sizes:
        for gn in sizes:
            if gm * gn > tiles:
                continue
            # A is DOUBLE-BUFFERED, so a chunk may use only half of L1 A: the
            # sweep for chunk i is still reading while chunk i+1 fills, which
            # is what lets the array keep working through a fill. Spending the
            # whole of L1 A on one chunk buys a longer chunk and gives back the
            # overlap, and the overlap is worth more -- fill was 22.3% of the
            # machine's time with the array idle throughout.
            nk = min(l1_a // (banks * gm), l1_b // gn, k_blocks)
            if nk < 1:
                continue
            # Intensity, 2*gm*gn/(gm+gn), DISCOUNTED BY PADDING.
            #
            # The largest tile is not the best tile. A dimension that is not a
            # multiple of the block pads up to one, and the padding is real
            # work: at 64x128 a 300x300 GEMM pads to 320x384 and does 36.5%
            # more arithmetic than the problem contains. Ranked on intensity
            # alone this picks 64x128 anyway and hands back a third of the
            # machine.
            #
            # `useful` is the fraction of the padded problem that is the actual
            # problem, so the score is the intensity the shape will really see.
            # On shapes that already fit, `useful` is 1 and this reduces to
            # ranking on intensity -- which is why the 256-cube and 1024-cube
            # answers do not move.
            cand = TileShape(gm, gn, nk)
            score = 2 * gm * gn / (gm + gn)
            if m and k and n:
                pm = -(-m // cand.m) * cand.m
                pk = -(-k // cand.k) * cand.k
                pn = -(-n // cand.n) * cand.n
                score *= (m * k * n) / (pm * pk * pn)
            key = (round(score * 4096), nk, -abs(gm - gn))
            if best is None or key > best[0]:
                best = (key, cand)
    if best is None:
        raise ValueError("no tile fits the hardware limits")
    return best[1]


def grid(m_tiles, n_tiles, ncl):
    """Which output tiles each cluster owns, as ``(mo, no)`` lists.

    THE UNIT OF CROSS-CLUSTER WORK IS AN OUTPUT TILE. Splitting N alone made
    the machine's width a property of one dimension: a problem with a narrow N
    ran on one cluster however much work it contained, and worse, the band was
    what `choose_tile` was asked to tile -- so a narrow band also picked a worse
    tile and multiplied the shortfall.

    COLUMN-MAJOR, IN CONTIGUOUS BLOCKS, and both halves matter:

    * column-major (``no`` outer, ``mo`` inner) keeps every m-tile that reads
      one band of B adjacent, so B stays resident across them exactly as it did
      when a cluster owned a whole band;
    * contiguous blocks mean a cluster's tiles are a run of that order, so it
      sees whole bands wherever the division allows -- and when ``ncl`` divides
      ``n_tiles`` this reproduces the old one-band-per-cluster split exactly,
      which is what makes the wide shapes provably unchanged.

    A ragged division gives the first clusters one tile more rather than
    rounding up for everyone: `ceil` would leave the last cluster idle.
    """
    tiles = [(mo, no) for no in range(n_tiles) for mo in range(m_tiles)]
    base, extra = divmod(len(tiles), ncl)
    owned, at = [], 0
    for ci in range(ncl):
        take = base + (1 if ci < extra else 0)
        owned.append(tiles[at : at + take])
        at += take
    return owned


@dataclass
class Pass:
    """One kick: fill, sweep K into the resident tile, drain once."""

    cluster: tuple  # (x, y)
    mo: int
    no: int
    flits: list = field(default_factory=list)
    text: list = field(default_factory=list)  # the kernel listing
    first_a: int = 0  # index of this pass's first A fill, for the prefetch
    first_a_len: int = 1  # how many flits it is, once split by the count field
    prefetch_len: int = 1  # flits moved ahead of the previous pass's barrier
    # Flits of the NEXT pass now sitting in this one, immediately before its
    # DRAIN. They are the next pass's prefetch, and a reader who does not know
    # that counts them against the wrong tile.
    lent: int = 0


@dataclass
class Round:
    """Passes that fit the staging window, the command RAM and the credit."""

    passes: list = field(default_factory=list)

    @property
    def nflits(self):
        return sum(len(p.flits) for p in self.passes)


@dataclass(frozen=True)
class L1Plan:
    """How one cluster's L1 is divided, and how long each operand stays in it.

    Recorded rather than recomputed by every reader: the two decisions are
    measured ones (see :func:`plan`) and a viewer that re-derives them from the
    capacities will disagree with the program the moment either changes.
    """

    a_entries: int  # capacity of the A side, in L1 entries
    b_entries: int  # capacity of the B side
    chunk_a: int  # entries one A chunk occupies, gm * nk
    chunk_b: int  # entries one B chunk occupies, gn * nk
    banks_a: int  # A chunks live at once
    banks_b: int  # B chunks live at once, when B is not resident
    b_resident: bool  # every K chunk of one column band fits, so B is filled once
    chunks: int  # K chunks per output tile
    fused: bool  # the sweep emits sub-tiles and DRAIN is only a barrier


@dataclass
class Kernel:
    """A tiled GEMM: what runs, in what order, cut into rounds."""

    tile: TileShape
    rounds: list
    # M, K, N -- the project's shape order everywhere. A GEMM is
    # C[M,N] = A[M,K] @ B.T[N,K], so K is the contracted dimension and naming
    # it in the middle is what makes the three-int form readable.
    m: int
    k: int
    n: int
    clusters: list
    layout: object  # matmul.Layout -- ONE image, shared by every cluster
    preq: tuple = (False, False)  # (A, B) already MXFP7 in memory
    l1: L1Plan = None

    @property
    def passes(self):
        return [p for r in self.rounds for p in r.passes]

    def listing(self, limit=None):
        """The kernel as text -- the tiling, readable."""
        out = [
            (
                f"  GEMM  C[{self.m},{self.n}] = A[{self.m},{self.k}] @ B.T"
                f"[{self.n},{self.k}]"
            ),
            (
                f"  tile  {self.tile} per cluster per pass "
                f"({self.tile.gm}x{self.tile.gn} sub-tiles, {self.tile.nk} K blocks)"
            ),
            (
                f"  {len(self.passes)} passes over "
                f"{len({p.cluster for p in self.passes})} of "
                f"{len(self.clusters)} clusters, {len(self.rounds)} rounds"
            ),
            "",
        ]
        shown = 0
        for ri, rnd in enumerate(self.rounds):
            out.append(f"  round {ri}   {len(rnd.passes)} passes, {rnd.nflits} flits")
            for p in rnd.passes:
                if limit is not None and shown >= limit:
                    out.append("    ...")
                    return out
                out.append(
                    f"    pass -> cluster (x={p.cluster[0]}, "
                    f"y={p.cluster[1]})  output tile ({p.mo},{p.no})"
                )
                out += [f"      {t}" for t in p.text]
                shown += 1
        return out


def plan(
    m,
    k,
    n,
    clusters,
    layout,
    tile,
    anchor=None,
    stage_flits=128,
    ncmd=128,
    preq=(False, False),
    l1_a=64,
    l1_b=128,
):
    """Decompose the problem into passes, then cut the passes into rounds.

    Every operand dimension must already be padded to a whole tile; partial
    tiles would make each pass a different size and buy nothing, since the
    padding is zeros and a zero contributes nothing to a dot product.

    ``preq`` says whether A and B were quantised on their way into memory. It
    changes two things and nothing else: the stride between entries, and the
    bit the FILL carries. Mixed is the normal case for inference -- weights are
    uploaded once and pre-quantised, activations arrive per layer.

    ``l1_a`` / ``l1_b`` are the cluster's L1 capacities in entries. They decide
    the two things below, and this is the ONLY place either is chosen.
    """
    anchor = matmul.ANCHOR if anchor is None else anchor
    preq_a, preq_b = preq
    wpe_a = tensor.entry_words(preq_a)
    wpe_b = tensor.entry_words(preq_b)
    ncl = len(clusters)
    nk_total = k // tensor.KBLOCK

    m_tiles = m // tile.m
    n_tiles = n // tile.n
    chunks = nk_total // tile.nk
    entries_per_chunk_a = tile.gm * tile.nk
    entries_per_chunk_b = tile.gn * tile.nk
    owned = grid(m_tiles, n_tiles, ncl)

    # WHO SHARES A. There is ONE image of each operand and one of C; a cluster
    # is told which output tile to compute, not which slice of memory it owns.
    # So `layout` is the same for everyone and the tile indices are what differ
    # -- which is the only arrangement in which an output tile can be handed to
    # any cluster at all.
    #
    # Clusters do read byte-identical A entries at the same moment, but only
    # when the grid gives each of them a whole number of column bands: pass i
    # of every cluster is then the same (mo, ko). When `n_tiles` is smaller
    # than the cluster count they are working down a single band together and
    # it is B that coincides instead.
    #
    # Naming that set on the instruction lets MAG read those bytes from DRAM
    # once and quantise them once, instead of once per cluster for a
    # bit-identical result. It is the only saving here that GROWS with cluster
    # count -- everything else in the fetch path divides a constant.
    # NOT ARMED, and the reason is a real defect rather than caution.
    #
    # A follower consumes whatever arrives while it is in a FILL -- and it
    # cannot tell WHICH fill the arriving entry belongs to. If the leader runs
    # ahead and its shared A entries reach a follower that is still executing
    # its own FILL B, they are written into the B side of L1 at the tag's
    # address. Measured at 256-cube: the rate improved 85.1 -> 105.1 GFLOP/s
    # and the worst element went from 1.0 to 2.2e+02 relative to the MXFP7
    # model. Correct answers first.
    #
    # The fix is a RENDEZVOUS, not a bigger buffer: MAG must not start a shared
    # stream until every subscriber has asked for it, so being in the matching
    # FILL is guaranteed rather than hoped for. A follower's request is one
    # flit either way, so the traffic saving survives; what it costs is
    # whatever skew exists between the clusters' instruction streams, which is
    # a number worth measuring rather than a guess.
    #
    # The mechanism is built and reachable -- `_flit(peers=...)`, the CU's
    # leader election, MAG's multi-destination emitter. Only the driver's
    # decision to use it is withheld.
    a_share = [() for _ in clusters]

    # ---- how L1 is used, which is two decisions and both are measured -----
    #
    # BANKS. A `GEMM` now retires when the sweep STARTS, so the CU runs the
    # next FILL while the array is still working. That only helps if the fill
    # lands somewhere the sweep is not reading, so consecutive chunks alternate
    # between two halves of L1. Two is exactly enough and no more: a sweep
    # waits for the one before it, so at any moment only two chunks are live.
    # Without it the array idled through every fill -- 22.3% of the machine.
    #
    # RESIDENCY. B does not change across the m loop. Re-filling it per m-tile
    # was a quarter of ALL memory traffic (4,096 of 16,384 beats at 256-cube).
    # If every K chunk of B fits at once it is filled once per column band and
    # left there, and the m loop reads it in place.
    banks_a = 2 if 2 * entries_per_chunk_a <= l1_a else 1
    b_resident = chunks * entries_per_chunk_b <= l1_b
    banks_b = 1 if b_resident else (2 if 2 * entries_per_chunk_b <= l1_b else 1)

    # FUSED DRAIN. The last K chunk's sweep hands each sub-tile out as it
    # finishes it, so the drain costs no accumulator commands and no second
    # pass through the pipeline. The one shape it cannot serve is a sweep whose
    # last K block is also its first: that command would have to both open the
    # tile and emit it, and OP_LOAD_EMIT does not exist. Only a single chunk of
    # a single K block is such a shape, and it falls back to a real DRAIN.
    fused = chunks > 1 or tile.nk > 1

    def a_bank(i):
        return (i % banks_a) * entries_per_chunk_a

    def b_bank(i, ko):
        return (
            ko * entries_per_chunk_b
            if b_resident
            else (i % banks_b) * entries_per_chunk_b
        )

    def emit_fill(p, sel, word, wpe, n, eoff, preq, peers=()):
        """Append the FILLs for one contiguous run, and say how many.

        A run longer than the 8-bit streaming count becomes several
        instructions at consecutive `eoff`, which is the whole reason `eoff`
        makes the count field a non-issue: it bounds the INSTRUCTION, not the
        chunk. The extra cost is one descriptor round trip.
        """
        done, flits = 0, 0
        while done < n:
            cnt = min(n - done, FILL_MAX_ENTRIES)
            p.flits.append(
                matmul._flit(
                    matmul.OP_FILL,
                    addr=(word + done * wpe) * WB,
                    n=cnt,
                    sel=sel,
                    peers=peers,
                    preq=preq,
                    eoff=eoff + done,
                )
            )
            p.text.append(
                f"FILL {'B' if sel else 'A'}  {cnt} entries @ word "
                f"{word + done * wpe} -> L1 {eoff + done}"
                f"{'  pre-quantised' if preq else ''}"
            )
            done += cnt
            flits += 1
        return flits

    by_cluster = [[] for _ in clusters]
    for ci, (cx, cy) in enumerate(clusters):
        gi = 0  # sweeps issued by this cluster, which is what picks the bank
        held = None  # the column band B is currently holding, when resident
        for mo, no in owned[ci]:
            p = Pass(cluster=(cx, cy), mo=mo, no=no)
            # B for this column band, once, before the m-tiles that read it.
            # Emitted on the first pass that needs it -- L1 is not cleared
            # between passes or between rounds, so it stays until the band
            # changes. `grid` keeps a cluster's tiles column-major, so that is
            # once per band and not once per pass.
            if b_resident and no != held:
                for ko in range(chunks):
                    b_ent = (no * chunks + ko) * entries_per_chunk_b
                    emit_fill(
                        p,
                        1,
                        layout.b_word + b_ent * wpe_b,
                        wpe_b,
                        entries_per_chunk_b,
                        b_bank(0, ko),
                        preq_b,
                    )
                held = no
            for ko in range(chunks):
                # tile-major layout: (tile, chunk) is one contiguous run
                a_ent = (mo * chunks + ko) * entries_per_chunk_a
                b_ent = (no * chunks + ko) * entries_per_chunk_b
                ab, bb = a_bank(gi), b_bank(gi, ko)
                last_chunk = ko == chunks - 1
                if ko == 0:
                    p.first_a = len(p.flits)
                    p.first_a_len = -(-entries_per_chunk_a // FILL_MAX_ENTRIES)
                    # The FIRST SWEEP moves ahead of the barrier too, when
                    # there is one behind it. It only opens tiles whose
                    # finished values are already in the drain queue -- the
                    # fused emit put them there on the last accumulate -- so
                    # overwriting the tile memory is safe, and the pass gets
                    # its compute started while the previous pass's write-back
                    # is still draining. Only when B is resident, so the flits
                    # are contiguous (no B fill between).
                    p.prefetch_len = p.first_a_len + (
                        1 if (b_resident and chunks > 1) else 0
                    )
                # C IS ONE IMAGE ADDRESSED BY TILE, so the destination depends
                # on the tile and not on who computes it. That is what lets the
                # grid hand (mo, no) to any cluster: with a per-cluster C base
                # the output's address changed when the owner did.
                c_word = layout.c_word + (mo * n_tiles + no) * tile.gm * tile.gn
                emit_fill(
                    p,
                    0,
                    layout.a_word + a_ent * wpe_a,
                    wpe_a,
                    entries_per_chunk_a,
                    ab,
                    preq_a,
                    a_share[ci],
                )
                if not b_resident:
                    emit_fill(
                        p,
                        1,
                        layout.b_word + b_ent * wpe_b,
                        wpe_b,
                        entries_per_chunk_b,
                        bb,
                        preq_b,
                    )
                emit = fused and last_chunk
                p.flits.append(
                    matmul._flit(
                        matmul.OP_GEMM,
                        # An emitting sweep carries the destination: it starts
                        # producing sub-tiles long before the DRAIN behind it
                        # is decoded.
                        addr=(c_word * WB) if emit else 0,
                        gm=tile.gm,
                        gn=tile.gn,
                        nk=tile.nk,
                        anchor=anchor,
                        acc=(ko > 0),
                        aoff=ab,
                        boff=bb,
                        emit=emit,
                    )
                )
                p.text.append(
                    f"GEMM    {tile.gm}x{tile.gn} sub-tiles, {tile.nk} K "
                    f"blocks, A@{ab} B@{bb}"
                    + ("  accumulate" if ko else "  load")
                    + (f"  emit -> word {c_word}" if emit else "")
                )
                gi += 1
            # sub-tile t of the drain is (row group, col group) within the
            # output tile; C is stored one word per 4x4 sub-tile
            p.flits.append(
                matmul._flit(
                    matmul.OP_DRAIN,
                    addr=c_word * WB,
                    n=tile.gm * tile.gn,
                    anchor=anchor,
                    last=True,
                    fuse=fused,
                )
            )
            p.text.append(
                f"DRAIN   {tile.gm * tile.gn} sub-tiles -> word "
                f"{c_word}"
                + (
                    "   (barrier; the sweep already wrote them)"
                    if fused
                    else "   (written once)"
                )
            )
            by_cluster[ci].append(p)

    # ---- prefetch the next pass's first operand ---------------------------
    # A pass ends with a barrier that waits for its output tile to reach
    # memory, and the CU executes instructions in order -- so the next pass's
    # FILL sat behind that wait with the array idle. Moving it in FRONT of the
    # barrier costs nothing and overlaps it with both the emitting sweep and
    # the write-back behind it.
    #
    # Safe by the same banking argument as everywhere else: the fill goes to
    # the bank the CURRENT sweep is not reading, and the sweep before that one
    # has already finished. Not carried across column bands, where B changes
    # and the first thing the pass does is refill it.
    if banks_a > 1:
        for row in by_cluster:
            for prev, cur in itertools.pairwise(row):
                if cur.no != prev.no:
                    continue
                # The whole fill, however many instructions it was split into,
                # and the sweep behind it.
                for _ in range(cur.prefetch_len):
                    f = cur.flits.pop(cur.first_a)
                    t = cur.text.pop(cur.first_a)
                    prev.flits.insert(len(prev.flits) - 1, f)
                    prev.text.insert(len(prev.text) - 1, t)
                    prev.lent += 1

    # INTERLEAVE the clusters. Rounds are cut from this list in order, so
    # emitting one cluster's passes and then the next would fill whole rounds
    # with a single cluster's work and leave the others idle through it --
    # N clusters would take N times as long as one, which is exactly what the
    # kick-all-then-wait-once dispatch exists to avoid. Round-robin keeps
    # every round balanced no matter where the cuts land.
    passes = [p for row in zip(*by_cluster) for p in row]
    done = len(passes) // len(clusters)
    passes += [p for col in by_cluster for p in col[done:]]  # ragged tail

    # ---- cut into rounds -------------------------------------------------
    # THREE bounds, and whichever binds first is the one that matters; checking
    # only one is how a program silently overruns the resource it was supposed
    # to fit.
    #
    # STAGING WINDOW and COMMAND RAM. The command cost of a pass is not a
    # constant -- a kick only writes the registers that changed since the last
    # one -- so the cut asks the program builder rather than assuming a figure.
    # Guessing high wastes command RAM; guessing low overruns it.
    #
    # CREDIT. Each program permanently consumes one credit -- its last
    # instruction retires as SIG_BATCH_COMPLETE, which does not refill -- so a
    # round of P programs needs more than P credits to get its last flit out.
    # Credit is capped by the CU instruction FIFO depth, because exceeding that
    # deadlocks the mesh (see Program.seed_credits), so P is capped too. Being
    # cut here costs an extra round; being wrong here stops the machine with
    # nothing executed and no error.
    max_by_credit = dev.INST_DEPTH - 1
    rounds, cur, cur_flits = [], [], 0
    for p in passes:
        fits_stage = cur_flits + len(p.flits) <= stage_flits
        fits_credit = len(cur) + 1 <= max_by_credit
        fits_cmd = len(control(cur + [p])) <= ncmd
        if cur and not (fits_stage and fits_cmd and fits_credit):
            rounds.append(Round(cur))
            cur, cur_flits = [], 0
        cur.append(p)
        cur_flits += len(p.flits)
    if cur:
        rounds.append(Round(cur))

    return Kernel(
        tile=tile,
        rounds=rounds,
        m=m,
        k=k,
        n=n,
        clusters=list(clusters),
        layout=layout,
        preq=(preq_a, preq_b),
        l1=L1Plan(
            a_entries=l1_a,
            b_entries=l1_b,
            chunk_a=entries_per_chunk_a,
            chunk_b=entries_per_chunk_b,
            banks_a=banks_a,
            banks_b=banks_b,
            b_resident=b_resident,
            chunks=chunks,
            fused=fused,
        ),
    )


def _slots(passes):
    """Each pass's first staging slot, packed end to end."""
    starts, slot = [], 0
    for p in passes:
        starts.append(slot)
        slot += len(p.flits)
    return starts


def control(passes, prog=None):
    """The control half of a round: clear, seed credit, kick every pass, wait.

    The passes in a round have no dependency on each other, so they are all
    launched before any is awaited, and completion is a single count for the
    whole machine rather than a poll per cluster.

    Used both to MEASURE a candidate round and to build the real one, so the
    two can never disagree about what a round costs.
    """
    prog = dev.Program() if prog is None else prog
    prog.clear_done()
    prog.seed_credits(dev.INST_DEPTH)
    for p, base in zip(passes, _slots(passes)):
        prog.kick(*p.cluster, base=base, nflits=len(p.flits))
    prog.await_all(sum(len(p.flits) for p in passes))
    prog.done(0xC0DE)
    return prog


def programs(kern):
    """One loadable program per round: the staged flits, then the control."""
    out = []
    for rnd in kern.rounds:
        prog = dev.Program()
        for p, base in zip(rnd.passes, _slots(rnd.passes)):
            for i, f in enumerate(p.flits):
                prog.stage_flit(base + i, f)
        out.append(control(rnd.passes, prog))
    return out
