"""The L1 banking contract, checked without a simulator.

A `GEMM` retires when its sweep STARTS, so the CU runs the next `FILL` while
the array is still reading L1. Nothing in the hardware interlocks them -- the
driver owns the banking, and a fill that lands inside the running sweep's range
corrupts a few sub-tiles and reports nothing. `mx_cluster_mgr` has a
simulation-only `$display` for it, but reaching that costs a Vivado run; this
costs milliseconds and covers every shape.

The instructions are DECODED from the flits rather than read off the planner,
so this also pins the encoding: an unsized value in the wrong place shifts
every field below it and elaborates cleanly.
"""

import itertools

import pytest

from kohakutpu import bench, kernel, matmul, tensor

# docs/isa/cluster.md s2. Kept here in full rather than imported, so that a
# change to the layout has to be made in two places on purpose.
FIELDS = {
    "op": (252, 0xF),
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


def decode(flit):
    p = flit & ((1 << 256) - 1)
    return {name: (p >> sh) & mask for name, (sh, mask) in FIELDS.items()}


def _overlaps(a0, a1, b0, b1):
    return a0 < b1 and b0 < a1


SHAPES = [(256, 256, 256), (256, 256, 128), (128, 128, 256), (512, 256, 128)]
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
        addr = (drains[0] >> 218) & ((1 << 34) - 1)
        seen.append((addr, addr + i["n"] * 32))
    seen.sort()
    for (a0, a1), (b0, _) in itertools.pairwise(seen):
        assert a1 <= b0, f"two passes drain into the same C words: {a1} > {b0}"


def test_b_is_filled_once_per_column_band():
    """Residency is the point of GB, so check it is actually taken.

    B does not change across the m loop; re-filling it per m-tile was a quarter
    of all memory traffic at the 256-cube.
    """
    prob = bench.build(256, 256, 256)
    per = prob.n // prob.ncl
    tile = prob.tile
    bands = per // tile.n
    chunks = (prob.k // tensor.KBLOCK) // tile.nk

    by_cluster = {}
    for p in prob.kern.passes:
        by_cluster.setdefault(p.cluster, []).extend(p.flits)
    want = bands * chunks * tile.gn * tile.nk
    for cluster, flits in by_cluster.items():
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
            f"({bands} band(s) x {chunks} chunk(s)) -- B is being re-read "
            f"across the m loop"
        )
