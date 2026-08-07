"""Building problems for, and reading results from, the RTL bench.

The bench (tests/mas/mag_driver_tb.v) loads five files and dumps one:

    operands.hex   the HOST's FP16 image, in upload order
    uploads.hex    {src, dst, words, flags} -- the regions the host pushes
    setup.hex      {addr, data} the host writes directly -- instruction flits
    program.hex    the driver's control commands
    rounds.hex     {nsetup, ncmd} per round -- how to cut the two above
    result.hex     device memory after the run

THE OPERANDS ARRIVE THROUGH THE HARDWARE, not through a backdoor. The host
writes FP16 into MAG's memory window and MAG stores it -- verbatim, or
quantised into MXFP7 as it lands, depending on a marker on the write. That is
the path XDMA will use, so simulating it is the only way the upload is tested
at all; and it is what lets an operand be pre-quantised without the driver ever
constructing MXFP7.

Nothing about the problem is compiled in, which is what lets one elaborated
snapshot serve any shape :func:`check` accepts -- and that is what makes an
interactive session possible at all. See :mod:`kohakutpu.sim`.
"""

import os
from dataclasses import dataclass

import numpy as np

from kohakutpu import kernel, matmul, mxfp7, tensor

# Compile-time capacities of mag_driver_tb.v. A shape that exceeds one of
# these does not fail loudly in simulation -- it silently computes something
# else -- so the driver checks rather than trusting the caller.
# 8 MB. It was 2 MB, which capped the problem at 512x512x512 -- and the cap
# had become the measurement: the multi-CU loss is a roughly CONSTANT ~8k
# cycles of startup and drain per run, so the only way to see whether the
# design scales is to divide that overhead into more work. 512x1024x256 alone
# needs 45,056 words of the old 65,536.
WORDS = 262144  # axi_ram WORDS -- 8 MB
NCMD = 128  # main_orch NCMD, per round
STAGE_FLITS = 128  # agent staging window, per round
# TILES is 512 because that is what the tile memory ALREADY COSTS. A sub-tile
# is 352 bits and a BRAM36 port is 72, so the array is ceil(352/72) = 5
# primitives at ANY depth up to 512 -- measured, docs/compute/matmul-impl.md
# s3. Running 64 left 448 of 512 sub-tiles of paid-for depth unused, and the
# tile is the term in the arithmetic intensity:
#
#     MACs/byte = 2*gm*gn / (gm+gn)     8.0 at 8x8, 21.3 at 16x32
#
# Gm=16, Gn=32 is the demand equation's own answer -- mx_cluster_cu.v's header
# says a cluster needs 4(Gm+Gn)/(Gm*Gn) words/cycle, which is 1.000 at 8x8
# (exactly the port's capacity, zero margin) and 0.375 at 16x32.
#
# This is NOT a deep URAM accumulator: no URAM, no extra primitive, and the
# output block stays 64x128, so 32 clusters fill at a 724-wide problem.
TILES = 512  # mx_cluster_cu TILES: resident output sub-tiles
# L1 holds MORE than one chunk of each operand, and the two reasons differ.
#
# A: TWO BANKS of gm*nk = 64, so the sweep for chunk i reads one while chunk
#    i+1 fills the other. A GEMM retires when the sweep starts, so this is what
#    turns fill time into hidden time -- it was 22.3% of the machine with the
#    array idle throughout.
# B: EVERY K CHUNK of one column band, gn*nk_total = 256 at K=256 for gn=32.
#    B does not change across the m loop, and re-filling it per m-tile was a
#    quarter of all memory traffic.
#
# 384 entries x 928 bits of LUTRAM per cluster. It is LUTRAM by shape: 928 bits
# wide is 13 BRAM36 whichever depth is chosen, because the WIDTH sets the
# primitive count.
#
# GA = 256 was tried and MEASURED WORSE: it makes `nk` 8, so the whole of
# K = 256 is one sweep and the ~19-cycle cascade tail is paid four times per
# cluster instead of eight. It still lost -- 407.4 -> 399.7 pre-quantised,
# 345.6 -> 329.3 online.
#
# The reason is what the two-chunk shape was quietly buying: chunk 1's FILL
# hides inside chunk 0's 2,048-cycle sweep. One sweep per pass deletes that
# window, and the only fill left to overlap is the next pass's, which arrives
# at the end. Four saved tails are worth less than four lost windows.
#
# So `nk` is bounded by OVERLAP, not by capacity. Worth re-testing if the
# drain ever stops being the thing the barrier waits for.
L1_A_ENTRIES = 128  # mx_cluster_cu GA
L1_B_ENTRIES = 256  # mx_cluster_cu GB
L1_BANKS = 2  # A chunks live at once
L1_ENTRIES = L1_A_ENTRIES  # kept for callers that assume one number
# ONE CLUSTER PER MESH ROW, manager at (1,y) and accumulator at (2,y) --
# mag_driver_tb builds exactly that shape and must be compiled with the same
# count, which `sim.Session.build` passes as `-d NCL=`.
#
# The default is 2 because that is what every number in .plans/ was measured
# on. `KOHAKU_NCL=8` re-elaborates for eight, which is the "8 CU per SLR"
# architecture -- and note the shape has to grow with it: N must be a multiple
# of NCL * tile.n or the clusters are not all fed.
#   NCL = 2   2x2 mesh, HALF = 1   managers at (1,1) (1,2)
#   NCL = 4   4x2 mesh, HALF = 2   managers at (1,1) (2,1) (1,2) (2,2)
#   NCL = 8   4x4 mesh, HALF = 2   ... rows 1..4
# A square-ish mesh, not a strip: half the hops to memory on the far side at
# eight clusters, and the shape an SLR actually lays out.
NCLUSTER_ENV = int(os.environ.get("KOHAKU_NCL", "2"))

# mag's memory-port coordinates are MEM_X/MEM_X1/MEM_X2/MEM_X3, so the mesh has
# at most four rows and therefore at most four memory ports.
MAX_MEM_ROWS = 4


def _grid(ncl):
    """(columns, clusters per band, rows) for a cluster count.

    MANAGERS OUTSIDE, ACCUMULATORS INSIDE -- two dataflow rings back to back::

        MAG  mgr mgr mgr mgr      row 1     band 0
        MAG  acu acu acu acu      row 2
        MAG  acu acu acu acu      row 3     band 1
        MAG  mgr mgr mgr mgr      row 4

    A cluster is a COLUMN of one band, so its two endpoints are on ADJACENT
    routers rather than straddling another cluster's node.
    """
    ncol = min(4, ncl)
    bands = (ncl + ncol - 1) // ncol
    return ncol, ncol, bands * 2


# The counts whose mesh the bench can actually generate: the managers have to
# divide evenly into the left half of the columns, and every row needs a memory
# port of its own.
CLUSTER_COUNTS = tuple(
    n for n in range(1, 2 * MAX_MEM_ROWS + 1) if _grid(n)[2] <= MAX_MEM_ROWS
)
# EVERY COUNT UP TO THE ROW BUDGET, not just the ones that tile evenly. A band
# that is not full is still a mesh the bench can generate -- the spare columns
# simply have no cluster on them -- so 3, 5, 6 and 7 are SUPPORTED, merely not
# optimal. Excluding them said "the hardware cannot" when the truth was "the
# work divides badly", and that is a driver's problem to report, not a mesh
# generator's to forbid.


def mesh(ncl):
    """The router grid, cluster endpoints and MAG's ports at this count.

    Mirrors the generate block in tests/mas/mag_driver_tb.v: NCOL columns of
    routers by NROW rows, cluster i at column ``i % HALF`` of row ``i // HALF``
    with its manager on the left half and its accumulator directly across, and
    one MAG memory port west of each row.

    THERE IS NO AGENT NODE. It used to sit east of row 1 -- one module attached
    at both edges of the mesh, with every dispatch through a single link. The
    agent shares the memory ports now and answers at port 0's coordinate, so a
    picture with an agent box in it is a picture of hardware that no longer
    exists.

    Derived here rather than in a viewer, for the same reason :func:`plan`
    derives the memory map here: a picture of a mesh the machine does not have
    still looks like a mesh.
    """
    if ncl not in CLUSTER_COUNTS:
        raise ValueError(
            f"{ncl} clusters is not a mesh this bench builds; "
            f"use one of {list(CLUSTER_COUNTS)}"
        )
    ncol, per_band, nrow = _grid(ncl)

    def _cluster(i):
        band = i // ncol
        cx = i % ncol
        # Band 0 hugs the top, band 1 is MIRRORED against the bottom, so the
        # accumulators meet in the middle and every manager is on an outer row.
        mrow = 1 if band == 0 else nrow
        arow = 2 if band == 0 else nrow - 1
        # Which row's edge this cluster's memory traffic exits by. The port is
        # on the NoC, not on a cluster, so this is a routing choice rather than
        # an ownership one -- near columns exit by the manager's row, far ones
        # by the accumulator's, which keeps both of a band's ports carrying.
        prow = mrow if cx < (ncol + 1) // 2 else arow
        return {
            "mgr": [cx + 1, mrow],
            "acu": [cx + 1, arow],
            "port": [0, prow],
            "row": mrow,
        }

    return {
        "ncl": ncl,
        "ncol": ncol,
        "nrow": nrow,
        # Clusters per band -- a band is the two rows one ring occupies.
        "half": per_band,
        # Port p is west of row p+1. Both of a cluster's endpoints reach it by
        # walking west along their own row, so no flit crosses into another
        # port's row.
        "ports": [[0, y + 1] for y in range(nrow)],
        # The agent answers HERE, on port 0, rather than at a node of its own.
        "agent_port": [0, 1],
        "clusters": [_cluster(i) for i in range(ncl)],
    }


def cluster_list(ncl):
    """Manager coordinates, which is what a kernel dispatches to."""
    return [tuple(c["mgr"]) for c in mesh(ncl)["clusters"]]


PACKINGS = ("spread", "packed")


def dispatch(mesh_ncl, use=None, packing="spread"):
    """Which of a mesh's clusters a program kicks, and what that costs.

    NCL is compiled into the bench, but WHICH clusters a program dispatches to
    is just data -- so a subset runs on the machine that is already elaborated,
    with the rest idle. That is what lets the cluster count be a control rather
    than a fifteen-second rebuild.

    THE SUBSET IS CHOSEN BY ROW, BECAUSE ROWS ARE WHAT OWN MEMORY PORTS. MAG has
    one port per mesh row (:func:`mesh`), so at NCL=8, where ``half`` is 2,
    clusters 0 and 1 share a port and 2 and 3 share the next. Taking the first
    ``use`` by index therefore packs them onto as few ports as possible:

        use=2 packed  ->  clusters 0,1  -- one port between them
        use=2 spread  ->  clusters 0,2  -- one port each

    Neither is wrong, and the difference is the whole argument for per-row
    ports, which is why both are offered. But the DEFAULT is spread, because
    packed silently halves the memory bandwidth per cluster and would read as
    the cluster count being at fault.

    Note a subset of a large mesh is still not the same machine as a small
    build: 2 of 8 spread has one port each, like the NCL=2 build, but its flits
    travel a 4x4 mesh rather than a 2x2 one.
    """
    cl = mesh(mesh_ncl)["clusters"]
    use = len(cl) if use is None else int(use)
    if not 1 <= use <= len(cl):
        raise ValueError(
            f"cannot dispatch to {use} of a {len(cl)}-cluster mesh; use 1..{len(cl)}"
        )
    if packing not in PACKINGS:
        raise ValueError(f"unknown packing {packing!r}; use one of {list(PACKINGS)}")

    if packing == "packed":
        pick = list(range(use))
    else:
        # Round-robin across rows: take one cluster from each row before taking
        # a second from any, so ports are claimed before they are shared.
        rows = {}
        for i, c in enumerate(cl):
            rows.setdefault(c["row"], []).append(i)
        order = []
        for depth in range(max(len(v) for v in rows.values())):
            for r in sorted(rows):
                if depth < len(rows[r]):
                    order.append(rows[r][depth])
        pick = order[:use]

    # Sorted, so a cluster's position in this list -- which is what binds it to
    # a column band -- does not depend on the packing that chose it.
    pick.sort()
    used_ports = {tuple(cl[i]["port"]) for i in pick}
    return {
        "mesh_ncl": len(cl),
        "use": use,
        "packing": packing,
        "index": pick,
        "clusters": [tuple(cl[i]["mgr"]) for i in pick],
        "rows": [cl[i]["row"] for i in pick],
        "ports": sorted(used_ports),
        # Clusters per active port. 1.0 means every dispatched cluster has a
        # memory port to itself; 2.0 means each port is feeding two.
        "per_port": use / len(used_ports),
    }


def dispatch_list(mesh_ncl, use=None, packing="spread"):
    """Just the manager coordinates, for callers that want the kernel's view."""
    return dispatch(mesh_ncl, use, packing)["clusters"]


CLUSTERS = cluster_list(NCLUSTER_ENV)
NCLUSTERS = len(CLUSTERS)


def use_clusters(ncl):
    """Set the machine's cluster count, rebinding CLUSTERS and NCLUSTERS.

    NCL is compiled into the bench, so this only describes the machine the next
    snapshot will be built for -- :meth:`kohakutpu.sim.Session.run` is what
    notices the snapshot went stale and re-elaborates.
    """
    global CLUSTERS, NCLUSTERS
    CLUSTERS = cluster_list(ncl)
    NCLUSTERS = ncl
    return CLUSTERS


# The bench's file-sized arrays, spanning ALL rounds concatenated. A pass costs
# only a kick in the PROGRAM -- a PROG_STAT poll, the dispatch registers that
# actually changed, and the kick itself -- because its instruction flits are
# setup data uploaded straight into the staging RAM and take no command slot.
# See kernel.control for the exact count.
MAX_SETUP = 1 << 18  # setup.hex lines; one per 64-bit host write
MAX_CMDS = 8192  # program.hex lines
MAX_ROUNDS = 512  # rounds.hex lines
MAX_HOST = 1 << 18  # operands.hex lines -- the host image, FP16
MAX_UPLOADS = 64  # uploads.hex lines -- one per contiguous region

# PRE-QUANTISED BY DEFAULT, both operands. The conversion happens once per
# tensor on the upload path instead of once per read on the fetch path, which
# is the difference between paying it O(passes) times and O(1). The driver
# still never builds MXFP7 -- it marks the upload and the hardware converts.
#
# Mixed is supported and is the real inference case: `preq=(False, True)` is
# activations produced this layer against weights uploaded once.
PREQ = (True, True)

# mx_cluster_cu's WBURST: sub-tiles per drain write burst. The layout aligns
# every C region to it so a burst cannot straddle a 4 KB boundary -- axi_ram
# does not care and a real interconnect does, so simulation would never catch
# it.
DRAIN_BURST = 8

RTL = [
    "src/common/sync_fifo.v",
    "src/common/kohaku_sdpram.v",
    "src/kohakunoc/noc_inport.v",
    "src/kohakunoc/noc_outport.v",
    "src/kohakunoc/noc_router.v",
    "src/kohakunoc/noc_orchestrator.v",
    "src/kohakunoc/noc_cu_base.v",
    "src/kohakutpu/matmul/mx_mac.v",
    "src/kohakutpu/matmul/mx_tcu.v",
    "src/kohakutpu/matmul/mx_fpacc.v",
    "src/kohakutpu/matmul/mx_acu_fp.v",
    "src/kohakutpu/matmul/mx_cluster_core.v",
    "src/kohakutpu/matmul/mx_cluster_mgr.v",
    "src/kohakutpu/matmul/mx_cluster_node.v",
    "src/kohakutpu/matmul/mx_cluster_cu.v",
    "src/kohakuaxi/axi_xbar2.v",
    "src/kohakuaxi/main_orch.v",
    "src/kohakumas/mx_quant.v",
    "src/kohakumas/axi_ram.v",
    "src/kohakumas/mag_mem_port.v",
    "src/kohakumas/mag.v",
    "tests/mas/mag_driver_tb.v",
]


@dataclass
class Problem:
    """A padded matmul, its memory layout, and the rounds that compute it."""

    a: np.ndarray  # [M][K] padded to whole tiles
    bt: np.ndarray  # [N][K] padded, B transposed
    layouts: list
    kern: object  # kohakutpu.kernel.Kernel
    programs: list  # one dev.Program per round
    # WHICH clusters of WHICH mesh. Both are needed and they are not the same
    # number: `ncl` below is how many were DISPATCHED to, because there is one
    # layout per dispatched cluster, while the machine that was elaborated may
    # be larger and its idle clusters are still there. Drawing the mesh from
    # the dispatched count would show a machine that does not exist.
    disp: dict = None

    @property
    def m(self):
        return self.a.shape[0]

    @property
    def k(self):
        return self.a.shape[1]

    @property
    def n(self):
        return self.bt.shape[0]

    @property
    def ncl(self):
        """Clusters this program DISPATCHES to -- one layout each."""
        return len(self.layouts)

    @property
    def mesh_ncl(self):
        """Clusters the MACHINE has, dispatched to or not."""
        return self.disp["mesh_ncl"] if self.disp else len(self.layouts)

    @property
    def tile(self):
        return self.kern.tile

    @property
    def commands(self):
        """The first round's control program, for tools that show one."""
        cmds = {}
        for i, c in enumerate(self.programs[0].cmds):
            cmds[i] = list(c.words())
        return cmds

    def reference(self):
        return self.a.astype(np.float64) @ self.bt.astype(np.float64).T


def padded_shape(m, n, k, tile, ncl=None):
    """Round the problem up to whole tiles.

    Partial tiles would make every pass a different size and buy nothing: the
    padding is zeros, and a zero contributes nothing to a dot product nor to a
    block's scale.

    N rounds up to a multiple of ``ncl * tile.n``, which is what feeds every
    cluster a whole column band.
    """
    ncl = len(CLUSTERS) if ncl is None else ncl

    def up(v, q):
        return ((v + q - 1) // q) * q

    per = up(up(n, ncl) // ncl, tile.n)
    return up(m, tile.m), per * ncl, up(k, tile.k)


def _up(v, q):
    return ((v + q - 1) // q) * q


def _tile_for(m, n, k, ncl):
    """The pass shape this problem will run with.

    ONE definition and three callers -- :func:`check`, :func:`preview` and
    :func:`build`. A preview that chose its own tile would describe a different
    machine from the one that runs, which is worse than showing nothing.
    """
    return kernel.choose_tile(
        TILES,
        L1_A_ENTRIES,
        max(1, k // tensor.KBLOCK),
        L1_B_ENTRIES,
        m=m,
        n=n // ncl,
        k=k,
        banks=L1_BANKS,
    )


def _host_words(m, n, k):
    """Words of `operands.hex` a padded problem needs.

    The host pushes FP16 whatever the STORED format is, so this does not shrink
    when the operands are pre-quantised.
    """
    nk = k // tensor.KBLOCK
    return (m // tensor.LANES + n // tensor.LANES) * nk * (
        tensor.FP16_ENTRY_BYTES // 32
    )


def plan(m, n, k, ncl, tile, preq=PREQ):
    """Where A, each cluster's B slice, and each cluster's C land, in words.

    Sizes come from the TILED layout AND from each operand's stored format, so
    they must be derived the same way the packer walks it -- a mismatch here
    overlaps two operands and the answer is quietly wrong.
    """
    per = n // ncl
    nk = k // tensor.KBLOCK
    a_words = (m // tensor.LANES) * nk * tensor.entry_words(preq[0])
    b_words = (per // tensor.LANES) * nk * tensor.entry_words(preq[1])
    # C is one word per 4x4 sub-tile, drained DRAIN_BURST words at a time.
    c_words = _up((m // tensor.LANES) * (per // tensor.LANES), DRAIN_BURST)

    b0 = a_words
    c0 = _up(b0 + b_words * ncl, DRAIN_BURST)
    return (
        [
            matmul.Layout(a_word=0, b_word=b0 + c * b_words, c_word=c0 + c * c_words)
            for c in range(ncl)
        ],
        c0 + c_words * ncl,
    )


def check(m, n, k, ncl=None, preq=PREQ, use=None, packing="spread"):
    """Every reason this shape will not run, in plain words. Empty means go.

    Three things can stop it: a shape below one sub-tile, a device image larger
    than the bench RAM, and a host image larger than the upload file. Everything
    that used to be a limit -- output sub-tiles, L1 entries, program length --
    is handled by tiling and streaming, so it bounds the number of passes rather
    than the problem.

    ``ncl`` is the MESH -- the machine that was elaborated -- and ``use`` is how
    many of its clusters the program actually kicks, defaulting to all of them.
    EVERY LIMIT BELOW IS A PROPERTY OF ``use``, NOT OF THE MESH: it is the
    dispatched count that divides N and sizes each cluster's band, so a shape
    that fits when 8 clusters run can fail when 2 of the same 8 do. Checking
    against the mesh instead would reject legal shapes and admit illegal ones.
    """
    ncl = len(CLUSTERS) if ncl is None else ncl
    why = []
    if ncl not in CLUSTER_COUNTS:
        why.append(
            f"{ncl} clusters is not a mesh this bench builds; "
            f"use one of {list(CLUSTER_COUNTS)}"
        )
        return why
    use = ncl if use is None else int(use)
    if not 1 <= use <= ncl:
        why.append(f"cannot dispatch to {use} of a {ncl}-cluster mesh; use 1..{ncl}")
        return why
    if packing not in PACKINGS:
        why.append(f"unknown packing {packing!r}; use one of {list(PACKINGS)}")
        return why
    if m < tensor.LANES or n < tensor.LANES or k < tensor.KBLOCK:
        why.append(f"minimum shape is {tensor.LANES}x{tensor.LANES}x{tensor.KBLOCK}")
        return why

    tile = _tile_for(m, n, k, use)
    mp, np_, kp = padded_shape(m, n, k, tile, use)
    _, end = plan(mp, np_, kp, use, tile, preq)
    if end > WORDS:
        why.append(
            f"padded to {mp}x{np_}x{kp} the memory image needs {end} words, "
            f"the bench RAM has {WORDS}"
        )
    host = _host_words(mp, np_, kp)
    if host > MAX_HOST:
        why.append(
            f"padded to {mp}x{np_}x{kp} the host image is {host} words, "
            f"the bench upload file holds {MAX_HOST}"
        )
    return why


def preview(m, n, k, ncl=None, preq=PREQ, use=None, packing="spread"):
    """What this shape BECOMES, without building operands or simulating.

    Every number here comes from the functions the real build uses -- the same
    :func:`_tile_for`, :func:`padded_shape`, :func:`plan` and
    :func:`kohakutpu.kernel.plan` -- so a preview cannot describe a different
    machine from the one that would run. Deriving it independently for speed
    would make it fast and wrong, which on a control panel is worse than slow.

    Cheap enough to answer on every keystroke: no operand tensor is allocated
    and nothing is packed. What it costs is the flit build, which is what makes
    the pass and round counts exact rather than estimated.
    """
    ncl = len(CLUSTERS) if ncl is None else ncl
    use = ncl if use is None else int(use)
    why = check(m, n, k, ncl, preq, use, packing)
    out = {
        "asked": {"m": m, "n": n, "k": k},
        "ncl": ncl,
        "use": use,
        "packing": packing,
        "preq": [bool(preq[0]), bool(preq[1])],
        "why": why,
        "runnable": not why,
    }
    # `check` returns early on these, and each of them makes the tile search
    # itself undefined -- a zero column band divides by zero in the padding
    # discount. There is nothing to describe beyond the reason.
    if ncl not in CLUSTER_COUNTS or not 1 <= use <= ncl \
            or packing not in PACKINGS \
            or m < tensor.LANES or n < tensor.LANES or k < tensor.KBLOCK:
        return out

    disp = dispatch(ncl, use, packing)
    tile = _tile_for(m, n, k, use)
    mp, np_, kp = padded_shape(m, n, k, tile, use)
    layouts, end = plan(mp, np_, kp, use, tile, preq)
    kern = kernel.plan(
        mp, np_, kp, disp["clusters"], layouts, tile,
        stage_flits=STAGE_FLITS, ncmd=NCMD, preq=preq,
        l1_a=L1_A_ENTRIES, l1_b=L1_B_ENTRIES,
    )
    per = np_ // use
    # WHICH clusters, and how many memory ports they land on. The port count is
    # the part that is not obvious from `use` alone.
    out["dispatch"] = disp
    out.update(
        {
            "padded": {"m": mp, "n": np_, "k": kp},
            "tile": {
                "m": tile.m, "n": tile.n, "k": tile.k,
                "gm": tile.gm, "gn": tile.gn, "nk": tile.nk,
                "subtiles": tile.gm * tile.gn, "text": str(tile),
            },
            "band": per,
            "grid": {
                "m_tiles": mp // tile.m,
                "n_tiles": per // tile.n,
                "chunks": kern.l1.chunks,
            },
            "passes": len(kern.passes),
            "rounds": len(kern.rounds),
            "flits": sum(len(p.flits) for p in kern.passes),
            "l1": {
                "b_resident": kern.l1.b_resident,
                "banks_a": kern.l1.banks_a,
                "banks_b": kern.l1.banks_b,
                "fused": kern.l1.fused,
            },
            # Both bench limits, with the figure that is measured against them.
            # Overrunning either is what `why` reports, and seeing how close a
            # shape is to the wall is the difference between a control that
            # refuses and one that explains.
            "memory": {"words": end, "capacity": WORDS},
            "host": {"words": _host_words(mp, np_, kp), "capacity": MAX_HOST},
            # The padding bill, stated rather than buried: the machine really
            # does the padded arithmetic, so a rate quoted against it is a rate
            # for work the caller did not ask for.
            "macs": mp * np_ * kp,
            "asked_macs": m * n * k,
        }
    )
    return out


def operands(m, n, k, seed, dist="lowrank"):
    """Random operands of a chosen character.

    The distribution is not a detail. Under `normal` every output is a fully
    cancelled sum, so relative error is pinned at the operand's own precision
    no matter how large K grows -- the worst case that exists. Real weights and
    activations are correlated, and `lowrank` reproduces that: the signal
    accumulates coherently while quantisation noise does not, and the error
    falls away with K. Benchmarking on `normal` and reporting it as the
    format's behaviour overstates the error several fold.
    """
    rng = np.random.default_rng(seed)
    if dist == "normal":
        return rng.standard_normal((m, k)), rng.standard_normal((n, k))
    if dist == "uniform":
        return rng.uniform(-1, 1, (m, k)), rng.uniform(-1, 1, (n, k))
    if dist == "heavy":
        a = rng.standard_normal((m, k))
        bt = rng.standard_normal((n, k))
        a[:, :: tensor.KBLOCK] *= 16.0
        bt[:, :: tensor.KBLOCK] *= 16.0
        return a, bt
    # lowrank: correlated, like real weights and activations
    r = max(1, k // 16)
    w = rng.standard_normal((r, k))
    return rng.standard_normal((m, r)) @ w, rng.standard_normal((n, r)) @ w


DISTRIBUTIONS = ("lowrank", "normal", "uniform", "heavy")


def build(m, n, k, seed=0xC0FFEE, ncl=None, dist="lowrank", preq=PREQ,
          use=None, packing="spread"):
    """A random problem of this shape, tiled into passes and cut into rounds.

    ``ncl`` is the elaborated mesh; ``use`` is how many of its clusters this
    program kicks. Everything that divides the problem keys off ``use``, because
    a cluster that is never kicked takes no column band.
    """
    ncl = len(CLUSTERS) if ncl is None else ncl
    use = ncl if use is None else int(use)
    tile = _tile_for(m, n, k, use)
    mp, np_, kp = padded_shape(m, n, k, tile, use)

    a0, bt0 = operands(m, n, k, seed, dist)
    a = np.pad(a0.astype(np.float32), ((0, mp - m), (0, kp - k)))
    bt = np.pad(bt0.astype(np.float32), ((0, np_ - n), (0, kp - k)))

    layouts, _ = plan(mp, np_, kp, use, tile, preq)
    kern = kernel.plan(
        mp,
        np_,
        kp,
        # DERIVED from the count, not sliced off the module global. Slicing
        # gave the first `ncl` of whatever the global happened to hold, so
        # `build(..., ncl=8)` against a global set for two silently planned a
        # two-cluster program and labelled it eight. `dispatch` also chooses
        # WHICH of the mesh's clusters, which is not the same as the first N.
        dispatch_list(ncl, use, packing),
        layouts,
        tile,
        stage_flits=STAGE_FLITS,
        ncmd=NCMD,
        preq=preq,
        l1_a=L1_A_ENTRIES,
        l1_b=L1_B_ENTRIES,
    )
    return Problem(
        a=a, bt=bt, layouts=layouts, kern=kern, programs=kernel.programs(kern),
        disp=dispatch(ncl, use, packing),
    )


def upload(prob):
    """The host's FP16 stream, and the regions of device memory it becomes.

    The host always sends FP16 -- that is the whole contract. A region is
    ``(src_word, dst_word, src_words, quant, blayout)``: where it sits in the
    host stream, where it lands, how many 256-bit source words it is, whether
    MAG quantises it on the way in, and which operand packing to use if it does.
    A quantised region occupies HALF as many device words as source words.

    TILE-MAJOR, so each pass's L1 entries are one contiguous run and a FILL is
    a single instruction. See tensor.to_fp16_words_tiled.
    """
    t = prob.tile
    preq_a, preq_b = prob.kern.preq
    words, regions = [], []

    src = tensor.to_fp16_words_tiled(prob.a, t.gm, t.nk)
    regions.append((0, prob.layouts[0].a_word, len(src), int(preq_a), 0))
    words += src

    per = prob.n // prob.ncl
    for c, lay in enumerate(prob.layouts):
        chunk = prob.bt[c * per : (c + 1) * per]
        src = tensor.to_fp16_words_tiled(chunk, t.gn, t.nk)
        regions.append((len(words), lay.b_word, len(src), int(preq_b), 1))
        words += src
    return words, regions


def write_files(work, prob):
    """Emit the five files the bench reads at time 0.

    operands.hex  the host's FP16 image, in upload order
    uploads.hex   {src, dst, words, flags} per region
    setup.hex     {addr, data} the host writes directly, all rounds concatenated
    program.hex   control commands, all rounds concatenated
    rounds.hex    {nsetup, ncmd} per round -- how to cut the two above
    """
    host, regions = upload(prob)
    for got, cap, what in (
        (len(host), MAX_HOST, "host image words"),
        (len(regions), MAX_UPLOADS, "upload regions"),
    ):
        if got > cap:
            raise ValueError(f"{got} {what} exceeds the bench's {cap}")
    (work / "operands.hex").write_text("\n".join(f"{v:064x}" for v in host) + "\n")
    (work / "uploads.hex").write_text(
        "\n".join(
            f"{s:08x}{d:08x}{w:08x}{(q | (b << 1)):08x}"
            for s, d, w, q, b in regions
        )
        + "\n"
    )

    setup, cmds, rounds = [], [], []
    for prog in prob.programs:
        rounds.append((len(prog.setup), len(prog.cmds)))
        setup += [f"{a:016x}{d:016x}" for a, d in prog.setup]
        cmds += [
            f"{sum(w << (64 * f) for f, w in enumerate(c.words())):064x}"
            for c in prog.cmds
        ]
        if len(prog.cmds) > NCMD:
            raise ValueError(f"round needs {len(prog.cmds)} commands, RAM has {NCMD}")
    # The arrays are still fixed-size in the bench, so overflowing one is a
    # real error even though the files are no longer padded to fill it.
    for got, cap, what in (
        (len(setup), MAX_SETUP, "setup writes"),
        (len(cmds), MAX_CMDS, "commands"),
        (len(rounds), MAX_ROUNDS, "rounds"),
    ):
        if got > cap:
            raise ValueError(f"{got} {what} exceeds the bench's {cap}")

    # NOT padded to the array size. The bench zeroes each array before it
    # reads, so a short file leaves the tail at zero -- and a round with zero
    # commands is already the end marker. Padding cost 9 MB of zeros written
    # and $readmemh'd on EVERY run, which is pure latency in an interactive
    # session where the whole point is that a run takes seconds.
    (work / "setup.hex").write_text("\n".join(setup) + "\n")
    (work / "program.hex").write_text("\n".join(cmds) + "\n")
    rl = [f"{ns:016x}{nc:016x}" for ns, nc in rounds]
    (work / "rounds.hex").write_text("\n".join(rl) + "\n")


def read_result(work, prob):
    """Decode result.hex into a [M][N] float array.

    C is stored per output TILE: each pass drains gm*gn words, one per 4x4
    sub-tile, in the manager's sweep order (row group major). So the decode
    has to walk tiles the same way the kernel emitted them.
    """
    words = [
        0 if "x" in w else int(w, 16) for w in (work / "result.hex").read_text().split()
    ]
    t = prob.tile
    per = prob.n // prob.ncl
    n_tiles = per // t.n
    got = np.zeros((prob.m, prob.n))

    for c, lay in enumerate(prob.layouts):
        for mo in range(prob.m // t.m):
            for no in range(n_tiles):
                base = lay.c_word + (mo * n_tiles + no) * t.gm * t.gn
                for st in range(t.gm * t.gn):
                    g, h = divmod(st, t.gn)
                    w = words[base + st]
                    for i in range(tensor.LANES):
                        for j in range(tensor.LANES):
                            bits = (w >> ((i * tensor.LANES + j) * 16)) & 0xFFFF
                            got[
                                mo * t.m + g * tensor.LANES + i,
                                c * per + no * t.n + h * tensor.LANES + j,
                            ] = mxfp7.fp16_to_float(bits)
    return got
