"""Build a control program that computes ``C = A @ B.T`` on the machine.

This is the interesting half of the driver and it needs no hardware to test:
turning a matmul into a command list is a pure function, so it can be checked
by recording the transactions and comparing them against a known-good list.
"""

from dataclasses import dataclass

from ktpu.hw import device as dev
from ktpu.hw.mxfp7 import ANCHOR
from ktpu.hw.tensor import KBLOCK, LANES

# CU instruction opcodes (mx_cluster_cu.v)
OP_FILL = 1
OP_GEMM = 2
OP_DRAIN = 3

FLIT_BITS = 288


def _flit(
    op,
    addr=0,
    n=0,
    sel=0,
    gm=0,
    gn=0,
    nk=1,
    anchor=ANCHOR,
    acc=False,
    last=False,
    peers=(),
    preq=False,
    eoff=0,
    aoff=0,
    boff=0,
    abank=0,
    bbank=0,
    fbank=0,
    emit=False,
    fuse=False,
    dnode=False,
    dst=(0, 0),
    dbuf=0,
    dflags=0,
    dack=(0, 0),
    dmesh=0,
    dfin=(0, 0),
):
    """Encode one CU_INST flit.

    Field widths matter more than they look: an unsized value in the wrong
    place shifts every field below it, silently. See docs/simulation.md s3.
    """
    # A FILL of 256 entries wraps the memory request's 8-bit streaming count to
    # 0, which memory coerces to 1 -- so the CU waits forever for entries
    # nobody asked for and the whole result is zeros, with nothing reported.
    # Refuse it here, where the count is chosen.
    if op == OP_FILL and n > 255:
        raise ValueError(
            f"FILL of {n} entries exceeds the 8-bit streaming count; "
            f"the request field must widen before a chunk this large"
        )
    v = 0
    v |= (0x5 & 0xF) << (FLIT_BITS - 16 - 4)  # type = CU_INST
    v |= (0x40 & 0xFF) << (FLIT_BITS - 20 - 8)  # txn
    v |= (1 if last else 0) << (FLIT_BITS - 28 - 1)
    # Payload layout. `n` is SIXTEEN bits: a 512-sub-tile resident tile means a
    # DRAIN of 512, and the 8-bit field it used to have wrapped silently at 256
    # -- draining the beginning of the tile a second time and reporting nothing.
    # The fields below it moved down rather than being overlapped with gm/gn,
    # because "this field means something else for that opcode" is how a
    # decode bug survives review.
    #
    #   [255:252] op   [251:218] addr(34)  [217:202] n(16)  [201] sel  [200] acc
    #   [199:192] gm   [191:184] gn        [183:176] nk     [175:168] anchor
    #   [167:144] peers(24)                [143:142] npeer  [141] preq
    #   [140:133] eoff  [132:125] aoff     [124:117] boff
    p = 0  # 256-bit payload
    p |= (op & 0xF) << 252
    p |= (addr & ((1 << 34) - 1)) << 218
    p |= (n & 0xFFFF) << 202
    p |= (sel & 1) << 201
    p |= (1 if acc else 0) << 200
    p |= (gm & 0xFF) << 192
    p |= (gn & 0xFF) << 184
    p |= (nk & 0xFF) << 176
    p |= (anchor & 0xFF) << 168
    # accumulate: chain this GEMM into the tile the last one left, instead of
    # reloading it. This is what lets K exceed L1 without the partial result
    # making a round trip through memory.
    # SHARED FETCH. The other clusters that read exactly these bytes at exactly
    # this moment, as {y,x} node indices. The lowest index in the set issues
    # the memory descriptor and the rest receive, so MAG reads the operand from
    # DRAM once and runs the quantiser over it once however many clusters want
    # it -- instead of once per consumer for a bit-identical result.
    #
    # Capped at three peers (four destinations) to match the RTL, which keeps
    # the emitter a fixed mux. That covers the real sharing pattern: with eight
    # clusters tiled 2x4 over the output, A is shared by the 2 in a row and B
    # by the 4 in a column.
    if len(peers) > 3:
        raise ValueError(f"at most 3 peers per fill, got {len(peers)}")
    # peer i at [144+8i +: 8], so the three of them fill [167:144] and the RTL
    # reads them as one 24-bit field with peer 0 in the low byte.
    for i, node in enumerate(peers):
        p |= (node & 0xFF) << (144 + i * 8)
    p |= (len(peers) & 0x3) << 142
    # PRE-QUANTISED. The operand was converted on the way INTO memory, so the
    # fetch reads 4 words per entry instead of 8 FP16 beats and skips the
    # quantiser. The driver still never constructs MXFP7: it says which tensors
    # were uploaded through the quantising window, and the hardware does the
    # rest. Per FILL, so weights can be pre-quantised while activations are not.
    p |= (1 if preq else 0) << 141
    # L1 IS ADDRESSABLE. `eoff` is where a FILL lands, `aoff`/`boff` where a
    # sweep reads. The driver owns the banking, and there is no interlock: a
    # fill that lands inside the running sweep corrupts a few sub-tiles and
    # reports nothing (the RTL has a simulation-only guard for it). See
    # kernel.plan, which is the only place these are chosen.
    p |= (eoff & 0xFF) << 133
    p |= (aoff & 0xFF) << 125
    p |= (boff & 0xFF) << 117
    # The NINTH bit of an L1 entry index: the offsets above are 8 bits, so entry
    # 256 wraps to 0 and two 256-entry chunks collide (docs/limits.md s6.8).
    p |= (abank & 1) << 114
    p |= (bbank & 1) << 113
    p |= (fbank & 1) << 112
    # FUSED DRAIN. `emit` makes the last K block of a sweep hand each sub-tile
    # out as it completes it, writing to the GEMM's own `addr`; `fuse` turns
    # the DRAIN that follows into a barrier that waits for them instead of
    # reading them all back. A separate drain needs one accumulator command per
    # sub-tile and the sweep has none spare, which is why it could not overlap.
    p |= (1 if emit else 0) << 116
    p |= (1 if fuse else 0) << 115
    # WHERE A DRAIN'S RESULTS GO. Zero is the memory port, which is what an
    # all-zero tail means -- so a DRAIN encoded before these fields existed
    # still writes to memory, and this whole block is inert for one.
    p |= (1 if dnode else 0) << 111
    p |= (dst[0] & 0xF) << 107
    p |= (dst[1] & 0xF) << 103
    p |= (dbuf & 0xFF) << 95
    p |= (dflags & 0xFF) << 87
    p |= (dack[1] & 0xF) << 83
    p |= (dack[0] & 0xF) << 79
    # ANOTHER MESH. `dfin` nonzero is what makes it remote -- (0,0) is a mesh
    # corner that can hold no endpoint, so it is an unambiguous sentinel. `dst`
    # then addresses the LOCAL MAG port and `dfin` the real destination in the
    # far mesh, which is what keeps the routers unaware that another mesh
    # exists. docs/interlink/transfers.md s5.
    fin = (dfin[1] & 0xF) << 4 | (dfin[0] & 0xF)
    if fin:
        if not dnode:
            raise ValueError(
                "a remote drain is still a node drain: set dnode=True, with "
                "dst pointing at this mesh's MAG port"
            )
        if not (dack[0] or dack[1]):
            raise ValueError(
                "a remote drain must name an ack destination. Across meshes "
                "ack=(0,0) means 'answer the sender', and the sender's "
                "coordinate exists in the DESTINATION mesh too -- so the "
                "completion would go to an unrelated local node. mag_ilink "
                "raises IL_F_ACK0 for this."
            )
    p |= (dmesh & 0x3) << 77
    p |= fin << 69
    return v | p


@dataclass
class Layout:
    """Where the operands and results live, in 256-bit memory words."""

    a_word: int
    b_word: int
    c_word: int


def cluster_program(a_entries, b_entries, gm, gn, nk, layout, anchor=ANCHOR):
    """The four CU instructions one cluster runs: FILL A, FILL B, GEMM, DRAIN."""
    wb = 32  # bytes per memory word
    return [
        _flit(OP_FILL, addr=layout.a_word * wb, n=a_entries, sel=0),
        _flit(OP_FILL, addr=layout.b_word * wb, n=b_entries, sel=1),
        _flit(OP_GEMM, gm=gm, gn=gn, nk=nk, anchor=anchor),
        _flit(OP_DRAIN, addr=layout.c_word * wb, n=gm * gn, anchor=anchor, last=True),
    ]


FLITS_PER_CLUSTER = 4  # FILL A, FILL B, GEMM, DRAIN


def remote_drain(board, n, dst_mesh, dst_node, ack_node, dbuf=2, dflags=1,
                 anchor=ANCHOR, last=True):
    """A DRAIN whose results land in a CU's L1 in ANOTHER mesh.

    `board` supplies the local MAG port coordinate and the mesh count; `dst_node`
    and `ack_node` are `(x, y)` in the DESTINATION mesh. `dbuf` is 2 because a
    cluster-to-cluster drain emits FP16 sub-tiles and L1 takes int7+E5M3 -- buf 2
    is the accumulator-float mode and nothing else selects it.

    Raises ValueError on a single-mesh board rather than encoding a flit whose
    remote bits that machine will not decode: the flit would reach MAG, be
    treated as an ordinary control flit, and be dropped.
    """
    if not board.multi_mesh:
        raise ValueError(
            f"board {board.name!r} is one mesh, so there is no mesh "
            f"{dst_mesh} to drain to. On single-mesh silicon the remote header "
            "bits are not decoded: the burst would reach MAG, be read as a "
            "control flit and be dropped, with the drop reported and the data "
            "gone. Drain locally, or point the driver at a multi-mesh board."
        )
    if dst_mesh == board.mesh_id:
        raise ValueError(
            f"mesh {dst_mesh} is this mesh; use an ordinary node drain"
        )
    return _flit(
        OP_DRAIN, n=n, anchor=anchor, last=last,
        dnode=True, dst=board.agent, dbuf=dbuf, dflags=dflags,
        dack=ack_node, dmesh=dst_mesh, dfin=dst_node,
    )


def build(m, k, n, clusters, layouts, anchor=ANCHOR, concurrent=True):
    """Whole-machine program for a problem that fits ONE pass per cluster.

    :mod:`ktpu.hw.kernel` is the general path -- it tiles, chains K and cuts
    rounds. This is the untiled case, kept because it is the smallest complete
    program the machine runs and the only place ``concurrent=False`` exists.

    ``clusters`` is a list of ``(x, y)`` manager coordinates; ``layouts`` gives
    each cluster's operand and result base words. Every cluster sweeps the
    whole of K over its own column slice, so the clusters share no data and
    none of them needs a peer reduction.

    Because they share no data they should also share no time. With
    ``concurrent`` every cluster is staged at its own base slot and kicked
    before any of them is awaited, so N clusters cost about what one costs.
    ``concurrent=False`` keeps the old stage-kick-wait-per-cluster order, which
    is only useful for showing what the serialisation costs.
    """
    gm = (m + LANES - 1) // LANES
    nk = (k + KBLOCK - 1) // KBLOCK
    gn = ((n // len(clusters)) + LANES - 1) // LANES

    prog = dev.Program()
    if not concurrent:
        for (x, y), lay in zip(clusters, layouts):
            for slot, flit in enumerate(
                cluster_program(gm * nk, gn * nk, gm, gn, nk, lay, anchor)
            ):
                prog.stage_flit(slot, flit)
            prog.dispatch(x, y, nflits=FLITS_PER_CLUSTER)
        prog.done(0xC0DE)
        return prog

    # stage every cluster's program into its own window, kick them all, then
    # wait -- the waits come last so nothing blocks a launch
    for c, ((x, y), lay) in enumerate(zip(clusters, layouts)):
        base = c * FLITS_PER_CLUSTER
        for slot, flit in enumerate(
            cluster_program(gm * nk, gn * nk, gm, gn, nk, lay, anchor)
        ):
            prog.stage_flit(base + slot, flit)
    prog.clear_done()
    prog.seed_credits(dev.INST_DEPTH)
    for c, (x, y) in enumerate(clusters):
        prog.kick(x, y, base=c * FLITS_PER_CLUSTER, nflits=FLITS_PER_CLUSTER)
    # ONE poll for every cluster. Per-node waits cost a command each, so the
    # program would grow with the machine -- and the program is what the host
    # pushes over AXI before anything runs.
    prog.await_all(len(clusters) * FLITS_PER_CLUSTER)
    prog.done(0xC0DE)
    return prog
