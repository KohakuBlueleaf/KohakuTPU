"""The hand-packed cluster encoder, kept as the ISA witness.

Its output is what the bitstream has executed, so `tests/test_ktpu_isa.py`
compares the declared field table against it bit for bit. Moved here when
`src/ktpu` was retired; the dispatch helpers it sat beside did not come.
"""

from kohakutpu.hw.mxfp7 import ANCHOR

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
    #   [167:144] peers(24)                [143:142] npeer  [141] reserved
    #   [140:133] eoff  [132:125] aoff     [124:117] boff   [68:63] addr_hi(6)
    p = 0  # 256-bit payload
    p |= (op & 0xF) << 252
    # SPLIT, and raise: masking to 34 dropped bit 39, aliasing staging onto DRAM.
    if not 0 <= addr < (1 << 40):
        raise ValueError(f"address {addr:#x} does not fit 40 bits")
    p |= (addr & ((1 << 34) - 1)) << 218
    p |= (addr >> 34) << 63
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
    # [141] is RESERVED and left 0. It was `preq`; every operand is already in
    # its final format before any fetch reads it, so a FILL has nothing to
    # select and the cluster no longer decodes the bit.
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
