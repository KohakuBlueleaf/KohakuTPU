"""The kernel program's contracts, checked without a simulator.

Three of them, and all three fail SILENTLY on hardware:

**Banking.** A `GEMM` retires when its sweep STARTS, so the CU runs the next
`FILL` while the array is still reading L1. Nothing in the hardware interlocks
them -- the driver owns the banking, and a fill that lands inside the running
sweep's range corrupts a few sub-tiles and reports nothing. `mx_cluster_mgr`
has a simulation-only `$display` for it, but reaching that costs a Vivado run;
this costs milliseconds and covers every shape.

**The dispatch grid.** Work is split across clusters by OUTPUT TILE `(mo, no)`
over one shared image of A, B and C. A tile computed twice, or not at all,
draws as a plausible matrix with one region stale or overwritten.

**Operand addressing.** The packer's ORDER is the driver's ADDRESSING, and they
are written in different modules against the same unstated contract. Neither
side can be checked by reading it, so the operands here carry a MARKER, the
real packer lays them out, and the flits are replayed against a model of L1: a
sweep reading slot `aoff + g*nk + kb` must find operand group `mo*gm + g` of K
block `ko*nk + kb`, or the machine multiplies whatever arrived and answers at
full speed.

The instructions are DECODED from the flits rather than read off the planner,
so this also pins the encoding: an unsized value in the wrong place shifts
every field below it and elaborates cleanly.
"""

import itertools

import numpy as np
import pytest

from ktpu.hw import bench, kernel, matmul, tensor

# docs/isa/cluster.md s2. Kept here in full rather than imported, so that a
# change to the layout has to be made in two places on purpose.
FIELDS = {
    "op": (252, 0xF),
    "addr": (218, (1 << 34) - 1),
    "n": (202, 0xFFFF),
    "sel": (201, 0x1),
    "acc": (200, 0x1),
    "gm": (192, 0xFF),
    "gn": (184, 0xFF),
    "nk": (176, 0xFF),
    "preq": (141, 0x1),
    "eoff": (133, 0xFF),
    "aoff": (125, 0xFF),
    "boff": (117, 0xFF),
    "emit": (116, 0x1),
    "fuse": (115, 0x1),
}

WB = 32  # bytes per memory word


def decode(flit):
    p = flit & ((1 << 256) - 1)
    return {name: (p >> sh) & mask for name, (sh, mask) in FIELDS.items()}


def _overlaps(a0, a1, b0, b1):
    return a0 < b1 and b0 < a1


# M, K, N -- the driver's shape order everywhere. These are the shapes for the
# BANKING checks, and they all have a fill behind a sweep to check; the grid
# shapes further down deliberately include one that does not.
SHAPES = [(256, 256, 256), (256, 128, 256), (128, 256, 128), (512, 128, 256)]
MODES = [(True, True), (False, False), (True, False), (False, True)]


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("preq", MODES)
def test_no_fill_lands_in_the_running_sweep(shape, preq):
    prob = bench.build(*shape, preq=preq)
    by_cluster = {}
    for p in prob.kern.passes:
        by_cluster.setdefault(p.cluster, []).extend(p.flits)

    checked = 0
    for cluster, flits in by_cluster.items():
        # At most one sweep is ever in flight: a GEMM waits for the one before
        # it, so only the most recent one can still be reading.
        live = None
        for flit in flits:
            i = decode(flit)
            if i["op"] == matmul.OP_GEMM:
                live = i
            elif i["op"] == matmul.OP_DRAIN:
                # A DRAIN ends the sweep in front of it, both ways round: the
                # issuing form waits for `!gemm_busy` before taking the
                # accumulator's mux, and the fused form waits for all gm*gn
                # sub-tiles, which only exist once the last K block ran.
                live = None
            elif i["op"] == matmul.OP_FILL and live is not None:
                checked += 1
                base = live["aoff"] if i["sel"] == 0 else live["boff"]
                span = (live["gm"] if i["sel"] == 0 else live["gn"]) * live["nk"]
                assert not _overlaps(
                    i["eoff"], i["eoff"] + i["n"], base, base + span
                ), (
                    f"{cluster}: FILL {'B' if i['sel'] else 'A'} "
                    f"[{i['eoff']},{i['eoff'] + i['n']}) lands inside the sweep "
                    f"reading [{base},{base + span})"
                )
    # A schedule with no fill behind a sweep would pass this vacuously, and
    # that is exactly the schedule the overlap exists to avoid.
    assert checked > 0, "no FILL follows a GEMM -- nothing overlaps"


@pytest.mark.parametrize("shape", SHAPES)
def test_fills_fit_l1_and_the_streaming_count(shape):
    prob = bench.build(*shape)
    cap = {0: bench.L1_A_ENTRIES, 1: bench.L1_B_ENTRIES}
    for p in prob.kern.passes:
        for flit in p.flits:
            i = decode(flit)
            if i["op"] != matmul.OP_FILL:
                continue
            # 256 wraps the request's 8-bit count to 0, which memory coerces to
            # 1 -- the CU then waits forever, in a legal state, silently.
            assert i["n"] <= kernel.FILL_MAX_ENTRIES
            assert i["eoff"] + i["n"] <= cap[i["sel"]], (
                f"FILL {'B' if i['sel'] else 'A'} runs past L1: "
                f"{i['eoff']} + {i['n']} > {cap[i['sel']]}"
            )


@pytest.mark.parametrize("shape", SHAPES)
def test_every_output_tile_is_drained_exactly_once(shape):
    """One DRAIN per pass, at the pass's own C words, and no overlap.

    Two passes writing the same C region is silent -- the second wins and the
    first tile is simply gone.
    """
    prob = bench.build(*shape)
    t = prob.tile
    seen = []
    for p in prob.kern.passes:
        drains = [f for f in p.flits if decode(f)["op"] == matmul.OP_DRAIN]
        assert len(drains) == 1
        i = decode(drains[0])
        assert i["n"] == t.gm * t.gn
        seen.append((i["addr"], i["addr"] + i["n"] * WB))
    seen.sort()
    for (a0, a1), (b0, _) in itertools.pairwise(seen):
        assert a1 <= b0, f"two passes drain into the same C words: {a1} > {b0}"


def test_b_is_filled_once_per_column_band():
    """Residency is the point of GB, so check it is actually taken.

    B does not change across the m loop; re-filling it per m-tile was a quarter
    of all memory traffic at the 256-cube.

    The band count is READ OFF each cluster's share of the grid, not divided
    out of N: the grid deals output tiles, so how many bands a cluster sees is
    a property of what it was given.
    """
    prob = bench.build(256, 256, 256)
    tile = prob.tile
    chunks = (prob.k // tensor.KBLOCK) // tile.nk

    by_cluster, bands = {}, {}
    for p in prob.kern.passes:
        by_cluster.setdefault(p.cluster, []).extend(p.flits)
        bands.setdefault(p.cluster, set()).add(p.no)
    for cluster, flits in by_cluster.items():
        want = len(bands[cluster]) * chunks * tile.gn * tile.nk
        # ENTRIES, not instructions: a run longer than the 8-bit streaming
        # count is several FILLs at consecutive eoff, which is a property of
        # the encoding and not of how many times B is read.
        nb = sum(
            decode(f)["n"]
            for f in flits
            if decode(f)["op"] == matmul.OP_FILL and decode(f)["sel"] == 1
        )
        assert nb == want, (
            f"{cluster}: {nb} B entries fetched, {want} are unique "
            f"({len(bands[cluster])} band(s) x {chunks} chunk(s)) -- B is "
            f"being re-read across the m loop"
        )


# --------------------------------------------------------- the dispatch grid
# M, K, N. Chosen for what they do to the GRID rather than for coverage of
# sizes: the K-heavy one is the shape that ran on a single cluster, the wide
# one is the recorded eight-cluster reference and must not move, and the
# smallest has fewer tiles than the machine has clusters.
GRID_SHAPES = [
    (256, 1024, 256),  # K-heavy: 4x2 tiles, the shape N-only dispatch starved
    (512, 256, 1024),  # the eight-cluster reference: 8x8 tiles
    (256, 256, 256),
    (128, 128, 128),
    (64, 128, 128),
    (16, 32, 16),  # one tile, so most clusters get nothing to do
]
NCLS = [1, 2, 4, 8]
# Both stored formats, because they differ in the STRIDE between entries and
# that is exactly what an address check can get wrong.
GRID_MODES = [(True, True), (False, False)]

WORDS_PER_FP16_ENTRY = tensor.FP16_ENTRY_BYTES // WB


def _marked(lanes, k):
    """A ``[lanes][K]`` operand whose every entry names itself.

    Entry ``(g, kb)`` is lanes ``4g..4g+3`` crossed with K ``32kb..32kb+31``.
    Lane 0 of its first K element holds ``g`` and lane 1 holds ``kb`` -- both
    small integers, so FP16 carries them exactly and a decoded entry says
    where in the tensor it came from.
    """
    x = np.zeros((lanes, k), dtype=np.float32)
    x[:: tensor.LANES, :: tensor.KBLOCK] = np.arange(lanes // tensor.LANES)[:, None]
    x[1 :: tensor.LANES, :: tensor.KBLOCK] = np.arange(k // tensor.KBLOCK)[None, :]
    return x


def _fp16(bits):
    return np.frombuffer(int(bits).to_bytes(2, "little"), dtype=np.float16)[0]


class Operand:
    """One packed operand, and what the packer put at each entry."""

    def __init__(self, words, src_word, dst_word, src_words, preq):
        self.words = words
        self.src = src_word
        self.dst = dst_word
        self.wpe = tensor.entry_words(preq)
        self.entries = src_words // WORDS_PER_FP16_ENTRY

    def marker(self, entry):
        """The ``(group, K block)`` the PACKER wrote at this entry.

        Read out of the packed image rather than recomputed, so this is the
        layout that reached memory and not a second copy of the formula that
        produced it.
        """
        w = self.src + entry * WORDS_PER_FP16_ENTRY
        # An entry is 4 lanes x 32 K, lane-major; a word is 16 FP16. So lane 0
        # element 0 is the low half of word 0, and lane 1 element 0 is the low
        # half of word 2.
        return (
            int(_fp16(self.words[w] & 0xFFFF)),
            int(_fp16(self.words[w + 2] & 0xFFFF)),
        )

    def entry_of(self, addr):
        """The entry a FILL's byte address names, or None if it names none."""
        word, rem = divmod(addr, WB)
        if rem or word < self.dst:
            return None
        entry, rem = divmod(word - self.dst, self.wpe)
        return None if rem or entry >= self.entries else entry


def _operands(prob):
    """Pack the marked problem through the real upload path."""
    prob.a = _marked(prob.m, prob.k)
    prob.bt = _marked(prob.n, prob.k)
    words, regions = bench.upload(prob)
    preq_a, preq_b = prob.kern.preq
    (sa, da, wa, *_), (sb, db, wb, *_) = regions
    return (
        Operand(words, sa, da, wa, preq_a),
        Operand(words, sb, db, wb, preq_b),
    )


def _cluster_flits(prob, cluster):
    """One cluster's instruction stream, in issue order.

    Passes are interleaved across clusters and cut into rounds, but a cluster's
    own order is preserved by both -- and it has to be read that way, because
    the prefetch moves a pass's first FILL and first sweep into the flit list
    of the pass BEFORE it. Grouping by pass would attribute them to the wrong
    tile.
    """
    return [f for p in prob.kern.passes if p.cluster == cluster for f in p.flits]


def _expected_sweeps(prob, cluster):
    """The (mo, no, ko) a cluster's sweeps must have, in order.

    Derived from the pass list rather than from where the flits ended up, for
    the same reason: a pass emits `chunks` sweeps in ko order whichever pass's
    list they were moved into.
    """
    chunks = prob.kern.l1.chunks
    return [
        (p.mo, p.no, ko)
        for p in prob.kern.passes
        if p.cluster == cluster
        for ko in range(chunks)
    ]


@pytest.mark.parametrize("ncl", NCLS)
@pytest.mark.parametrize("shape", GRID_SHAPES)
def test_every_output_tile_is_computed_exactly_once(shape, ncl):
    """The grid is a partition: no tile computed twice, none left out.

    A duplicate is not a wasted pass -- both write the same C words and the
    later one wins -- and a gap leaves whatever the memory held.
    """
    prob = bench.build(*shape, ncl=ncl)
    t = prob.tile
    want = {(mo, no) for mo in range(prob.m // t.m) for no in range(prob.n // t.n)}
    got = [(p.mo, p.no) for p in prob.kern.passes]
    assert len(got) == len(set(got)), "an output tile is computed twice"
    assert set(got) == want, "the grid does not cover C"
    # And it is really spread: every cluster works, up to the point where there
    # are fewer tiles than clusters and some must idle.
    assert len({p.cluster for p in prob.kern.passes}) == min(ncl, len(want))


@pytest.mark.parametrize("ncl", NCLS)
@pytest.mark.parametrize("shape", GRID_SHAPES)
def test_c_words_are_written_once_and_cover_the_region(shape, ncl):
    """Every DRAIN at its tile's address, disjoint, and filling C exactly."""
    prob = bench.build(*shape, ncl=ncl)
    t = prob.tile
    n_tiles = prob.n // t.n
    span = t.gm * t.gn
    seen = set()
    for p in prob.kern.passes:
        drains = [f for f in p.flits if decode(f)["op"] == matmul.OP_DRAIN]
        assert len(drains) == 1, "a pass must drain its tile once"
        d = decode(drains[0])
        want = prob.layout.c_word + (p.mo * n_tiles + p.no) * span
        assert d["addr"] // WB == want, (
            f"tile ({p.mo},{p.no}) drains to word {d['addr'] // WB}, "
            f"the layout puts it at {want}"
        )
        assert d["n"] == span
        words = set(range(want, want + span))
        assert not (words & seen), "two passes write the same C words"
        seen |= words
    assert seen == set(
        range(prob.layout.c_word, prob.layout.c_word + (prob.m // 4) * (prob.n // 4))
    ), "C is not covered exactly by the drains"


# The round trip below decodes C element by element in Python, so it is quoted
# on the shapes that vary the tile GEOMETRY rather than on the largest -- a
# 512x1024 answer is half a million elements and adds nothing the 4x2 grid does
# not already say.
ROUNDTRIP = [s for s in GRID_SHAPES if s != (512, 256, 1024)]


@pytest.mark.parametrize("ncl", [1, 8])
@pytest.mark.parametrize("shape", ROUNDTRIP)
def test_read_result_inverts_the_addresses_the_drains_carry(shape, ncl, tmp_path):
    """The decode must walk C the way the program wrote it.

    C has no second packer to check against -- the driver both writes the
    addresses and reads them back -- so what is provable here is that the two
    ends agree. The image below is built from the DRAIN flits themselves, so a
    decode that derives the tile base any other way reads the wrong words and
    returns a plausible matrix with its tiles transposed.
    """
    prob = bench.build(*shape, ncl=ncl)
    t = prob.tile
    words = [0] * (prob.layout.c_word + (prob.m // 4) * (prob.n // 4))
    for p in prob.kern.passes:
        drain = next(f for f in p.flits if decode(f)["op"] == matmul.OP_DRAIN)
        base = decode(drain)["addr"] // WB
        for st in range(t.gm * t.gn):
            g = st // t.gn  # the sub-tile's row group; its column does not
            w = 0  # matter, since every element carries its own row
            for i in range(tensor.LANES):
                for j in range(tensor.LANES):
                    row = p.mo * t.m + g * tensor.LANES + i
                    w |= int(np.float16(row).view(np.uint16)) << (
                        (i * tensor.LANES + j) * 16
                    )
            words[base + st] = w
    (tmp_path / "result.hex").write_text("\n".join(f"{v:064x}" for v in words) + "\n")
    got = bench.read_result(tmp_path, prob)
    want = np.repeat(np.arange(prob.m)[:, None], prob.n, axis=1)
    assert np.array_equal(got, want), "the C decode disagrees with the drains"


@pytest.mark.parametrize("preq", GRID_MODES)
@pytest.mark.parametrize("ncl", NCLS)
@pytest.mark.parametrize("shape", GRID_SHAPES)
def test_fills_name_the_entries_the_packer_wrote(shape, ncl, preq):
    """The whole operand contract, replayed: address, layout, banking, sweep.

    Every FILL must name entries inside its operand's packed region, and every
    slot a sweep reads must hold the entry that sweep's tile and K chunk need.
    That is the agreement between `tensor.to_fp16_words_tiled` and
    `kernel.plan` -- and it is the failure with no symptom, because the machine
    multiplies whatever arrived.
    """
    prob = bench.build(*shape, ncl=ncl, preq=preq)
    a_img, b_img = _operands(prob)
    t = prob.tile
    checked = 0

    for cluster in {p.cluster for p in prob.kern.passes}:
        l1 = ({}, {})  # sel 0 -> A, sel 1 -> B; slot -> (group, K block)
        sweeps = iter(_expected_sweeps(prob, cluster))
        for flit in _cluster_flits(prob, cluster):
            i = decode(flit)
            if i["op"] == matmul.OP_FILL:
                img = b_img if i["sel"] else a_img
                entry = img.entry_of(i["addr"])
                assert entry is not None, (
                    f"FILL {'B' if i['sel'] else 'A'} names byte {i['addr']}, "
                    f"which is not an entry of that operand's region"
                )
                assert entry + i["n"] <= img.entries, "FILL runs past the operand"
                for j in range(i["n"]):
                    l1[i["sel"]][i["eoff"] + j] = img.marker(entry + j)
            elif i["op"] == matmul.OP_GEMM:
                mo, no, ko = next(sweeps)
                for g in range(t.gm):
                    for kb in range(t.nk):
                        slot = i["aoff"] + tensor.entry_index(g, kb, t.nk)
                        assert l1[0].get(slot) == (mo * t.gm + g, ko * t.nk + kb), (
                            f"sweep of tile ({mo},{no}) chunk {ko} reads A slot "
                            f"{slot} expecting group {mo * t.gm + g} block "
                            f"{ko * t.nk + kb}, L1 holds {l1[0].get(slot)}"
                        )
                for h in range(t.gn):
                    for kb in range(t.nk):
                        slot = i["boff"] + tensor.entry_index(h, kb, t.nk)
                        assert l1[1].get(slot) == (no * t.gn + h, ko * t.nk + kb), (
                            f"sweep of tile ({mo},{no}) chunk {ko} reads B slot "
                            f"{slot} expecting group {no * t.gn + h} block "
                            f"{ko * t.nk + kb}, L1 holds {l1[1].get(slot)}"
                        )
                checked += 1
                # The emitting sweep carries the destination itself, so it has
                # to name the same tile the DRAIN behind it does.
                if i["emit"]:
                    want = (
                        prob.layout.c_word + (mo * (prob.n // t.n) + no) * t.gm * t.gn
                    )
                    assert i["addr"] // WB == want
        assert next(sweeps, None) is None, "a planned sweep was never issued"
    assert checked == len(prob.kern.passes) * prob.kern.l1.chunks
