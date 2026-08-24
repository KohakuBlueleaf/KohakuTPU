"""Operand layout: padding, FP16 packing, and the shapes the hardware assumes.

THE LAYOUT CONTRACT. Every operand tensor is stored row-major as FP16:

    [lanes][K]      lanes % 4 == 0,  K % 32 == 0

    A operand   lanes = M rows of A
    B operand   lanes = N columns of B, i.e. B stored TRANSPOSED
    C output    [M][N] row-major

The hardware assumes this and never checks it. That is affordable because
padding with zeros is numerically inert -- a zero element contributes nothing
to a dot product, and the block scale ignores it -- so the alternative
(masking, partial blocks, a scale that skips padding) would buy nothing.

Two consequences worth knowing:

* ``A @ B.T`` is what ``torch.nn.Linear`` already does, and its ``weight`` is
  stored ``(out_features, in_features)`` = ``[N][K]``. Weights upload verbatim,
  with no transpose.
* ``C[M][N]`` row-major is exactly the ``[lanes][K]`` shape the next layer wants
  as its A operand, so layer output feeds layer input with no relayout.
"""

import numpy as np

LANES = 4
KBLOCK = 32
FP16_ENTRY_BYTES = 256  # one L1 entry of SOURCE: 4 lanes x 32 FP16
# The same entry once converted: 4 lanes x 32 int7 + 4 E5M3 scales, packed as
# four 256-bit operand words. Half the bytes, and the form the mesh carries.
# These two are the transform slot's IN_BITS/OUT_WORDS in bytes -- the ratio the
# mover needs to size a converting move before the transform has run.
MXFP7_ENTRY_BYTES = 128

# An `entry_words(prequantised)` helper used to live here and pick between the
# two. It is gone: a fetch is never transformed, so what memory holds at a FILL's
# address is always the converted form and the choice does not exist. It had no
# callers, and its `False` branch would have sized a fetch for a format that is
# no longer in memory.


def pad_operand(x):
    """Pad a [lanes][K] operand up to the alignment the hardware assumes."""
    lanes, k = x.shape
    lp = (-lanes) % LANES
    kp = (-k) % KBLOCK
    if lp or kp:
        x = np.pad(x, ((0, lp), (0, kp)))
    return x


def entry_index(group, kblock, nk):
    """L1 entry number for lane-group `group` of K-block `kblock`.

    The manager's sweep reads A at ``g*nk + kb`` and B at ``h*nk + kb``
    (mx_cluster_mgr.v), so entries are group-major and K-block-minor. Memory
    order has to agree, because FILL just streams entries 0..n-1 in sequence.
    """
    return group * nk + kblock


def to_fp16_words(x, word_bits=256):
    """Serialise a padded [lanes][K] FP16 operand into memory words.

    One L1 entry is 4 lanes x 32 K, laid out lane-major -- all 32 K of lane 0,
    then lane 1, and so on. That is what mx_quant.v consumes. Entries follow
    :func:`entry_index`, so a lane group's K-blocks are adjacent; with K == 32
    that is the same as a plain row-major dump, which is why the ordering only
    starts to matter once K exceeds one block.
    """
    # group-major IS the tiled layout with one group per tile and one block
    # per chunk, so there is only one packer to get right
    return to_fp16_words_tiled(x, 1, 1, word_bits)


def to_fp16_words_tiled(x, groups_per_tile, blocks_per_chunk, word_bits=256):
    """Serialise a padded operand TILE-MAJOR, so each pass reads a run.

    A pass wants the L1 entries for one (tile, K-chunk): groups
    ``[t*gt, (t+1)*gt)`` crossed with K blocks ``[c*bc, (c+1)*bc)``. In the
    natural group-major order those are gt separate runs of bc entries, so a
    pass would need gt FILL instructions instead of one -- or the hardware
    would need a strided fetch.

    Reordering memory removes the problem instead of paying for it. Each entry
    still appears exactly once; only the order changes, and the driver owns
    the order. Layout is part of the kernel, not a property of the tensor.
    """
    lanes, k = x.shape
    assert lanes % LANES == 0 and k % KBLOCK == 0
    gt, bc = groups_per_tile, blocks_per_chunk
    g_total, nk_total = lanes // LANES, k // KBLOCK
    assert g_total % gt == 0, "pad the operand to whole tiles"
    assert nk_total % bc == 0

    per_word = word_bits // 16
    h = x.astype(np.float16).view(np.uint16)

    # (group, lane, block, k) -> (tile, chunk, group, block, lane, k), which is
    # exactly the order a pass consumes. Done as a transpose because a 512x512
    # operand is 16k entries and a Python loop over them is minutes, not
    # milliseconds.
    e = h.reshape(g_total, LANES, nk_total, KBLOCK)
    e = e.reshape(g_total // gt, gt, LANES, nk_total // bc, bc, KBLOCK)
    e = e.transpose(0, 3, 1, 4, 2, 5)
    flat = np.ascontiguousarray(e).reshape(-1, per_word)

    # a word IS its 16 uint16 little-endian, so the bytes are already correct
    return [int.from_bytes(row.tobytes(), "little") for row in flat]


def conv_geometry(h, w, pad=1, stride=1):
    """The padded plane one 32-channel block occupies: ``(hp, wp, positions)``.

    `positions` is ``hp*wp`` rounded up to a whole entry, because a channel
    block that ended mid-entry would put pixels of two different blocks in one
    entry's four lanes -- and an entry's lanes must be four adjacent pixels of
    ONE block for the tap offset below to mean anything.

    At `stride > 1` the block holds `stride*stride` SUB-PLANES of the residue
    split, each padded and entry-aligned on its own, and `hp`/`wp` are one
    sub-plane's. That is what makes a strided tap a constant again: see
    :func:`stride_tap`.
    """
    hs, ws = -(-h // stride), -(-w // stride)
    hp, wp = hs + 2 * pad, ws + 2 * pad
    return hp, wp, -(-(hp * wp) // LANES) * LANES * stride * stride


def tap_offset(dy, dx, wp):
    """Bytes from a pixel's 32-channel block to tap ``(dy, dx)``'s.

    THE POINT OF THE LAYOUT. In ``[C/32][plane][32]`` a pixel's channel block is
    64 contiguous bytes and adjacent pixels are adjacent, so shifting a 3x3 tap
    is adding a constant to the FILL address -- no stride, no im2col, no second
    copy of the activation. The step is 64 B: a QUARTER of an L1 entry.
    """
    return (dy * wp + dx) * KBLOCK * 2


def stride_tap(dy, dx, wp, sub, stride):
    """Tap ``(dy, dx)``'s offset in POSITIONS, for a strided convolution.

    Output `(oy, ox)` reads input `(s*oy + dy - pad, s*ox + dx - pad)`. Split
    that coordinate by residue -- `s*oy + dy - 1 = s*(oy + qy) + ry` -- and the
    row lands in sub-plane `ry` at sub-pixel `oy + qy`, with `qy` and `ry`
    depending on the TAP alone. So the offset is a constant, as at stride 1, and
    the whole difference is which sub-plane it lands in.

    The `+1`s are the sub-plane's own padding, and they are what keep the offset
    non-negative and the centre tap at `wp + 1` -- the same place stride 1 puts
    it, so `positions` reads the same for both.
    """
    qy, ry = divmod(dy - 1, stride)
    qx, rx = divmod(dx - 1, stride)
    return (ry * stride + rx) * sub + (qy + 1) * wp + (qx + 1)


def conv_planes(x, pad, stride):
    """``[C/32][plane][32]`` for an ``[H][W][C]`` activation, as an array.

    At `stride > 1` the block's plane is `stride*stride` sub-planes in residue
    order `ry*stride + rx`, which is the order :func:`stride_tap` addresses.
    """
    h, w, c = x.shape
    _, wp, plane = conv_geometry(h, w, pad, stride)
    sub, blocks = plane // (stride * stride), c // KBLOCK

    out = np.zeros((blocks, plane, KBLOCK), np.float16)
    for ry in range(stride):
        for rx in range(stride):
            part = np.asarray(x[ry::stride, rx::stride, :], np.float16)
            ph, pw, _ = part.shape
            where = (
                (np.arange(ph) + pad)[:, None] * wp + (np.arange(pw) + pad)
            ).ravel()
            at = (ry * stride + rx) * sub
            body = part.reshape(ph * pw, blocks, KBLOCK).transpose(1, 0, 2)
            out[:, at + where, :] = body
    return out


def to_fp16_words_conv(x, pad=1, tail=0, word_bits=256, stride=1):
    """Serialise an NHWC activation as the conv operand `[C/32][plane][32]`.

    `x` is ``[H][W][C]`` with ``C % 32 == 0``; the result is the same tensor
    with the 32-channel block as the OUTER axis and the pixel raster inside it,
    zero-padded by `pad` on both spatial axes. Four adjacent pixels of one
    channel block are then 256 contiguous bytes -- exactly one L1 entry -- so a
    fill is one run at `nk = 1` and a tap is :func:`tap_offset`.

    `tail` is entries of zeros appended, which the last channel block needs
    because the last TILE of the sweep reads a tap past the end of the plane.
    `stride` splits the plane by residue -- see :func:`conv_planes`.

    Raises :class:`ValueError` for a rank or a channel count the layout cannot
    hold.
    """
    x = np.asarray(x)
    if x.ndim != 3:
        raise ValueError(f"a conv activation is [H][W][C]; got {x.shape}")
    c = x.shape[2]
    if c % KBLOCK:
        raise ValueError(
            f"{c} channels is not a whole number of {KBLOCK}-element blocks, and "
            f"the block is this layout's outer axis; pad the channels first"
        )
    out = conv_planes(x, pad, stride)
    # The tail goes after the LAST block, not after each: the blocks are one
    # run, so an overrun in block b reads block b+1 and only the last runs off.
    flat = np.concatenate(
        [out.reshape(-1), np.zeros(tail * LANES * KBLOCK, np.float16)]
    ).view(np.uint16)

    per_word = word_bits // 16
    rows = np.ascontiguousarray(flat).reshape(-1, per_word)
    return [int.from_bytes(row.tobytes(), "little") for row in rows]


def from_fp16_words_conv(words, shape, pad=1, word_bits=256, stride=1):
    """The activation :func:`to_fp16_words_conv` packed, back as ``[H][W][C]``.

    Bit-exact: the packer only reorders and zero-fills, so nothing here rounds.
    """
    h, w, c = shape
    _, wp, plane = conv_geometry(h, w, pad, stride)
    sub, blocks = plane // (stride * stride), c // KBLOCK
    raw = b"".join(int(word).to_bytes(word_bits // 8, "little") for word in words)
    flat = np.frombuffer(raw, np.float16)[: blocks * plane * KBLOCK]
    held = flat.reshape(blocks, plane, KBLOCK)
    out = np.zeros((h, w, c), np.float16)
    for ry in range(stride):
        for rx in range(stride):
            ph, pw = len(range(ry, h, stride)), len(range(rx, w, stride))
            where = (
                (np.arange(ph) + pad)[:, None] * wp + (np.arange(pw) + pad)
            ).ravel()
            at = (ry * stride + rx) * sub
            got = held[:, at + where, :].transpose(1, 0, 2).reshape(ph, pw, c)
            out[ry::stride, rx::stride, :] = got
    return out


def to_mxfp7_words_tiled(x, groups_per_tile, blocks_per_chunk, blayout=0):
    """Serialise a padded operand as MXFP7 entries, in `to_fp16_words_tiled` order.

    One entry is four 256-bit words; word `w` carries K `[w*8, w*8+8)` as 32
    int7 at slot `lane*8 + kk`, MSB-first from bit 255, and repeats all four
    E5M3 scales in the low 32 bits at `31 - lane*8`. `blayout=1` transposes the
    slot map, which is the B-operand packing (`mx_quant.v`).

    Raises :class:`ValueError` unless the operand is padded to whole tiles.
    """
    from kohakutpu.hw import mxfp7

    lanes, k = x.shape
    gt, bc = groups_per_tile, blocks_per_chunk
    g_total, nk_total = lanes // LANES, k // KBLOCK
    if g_total % gt or nk_total % bc:
        raise ValueError("pad the operand to whole tiles before packing")

    q, es, m8 = mxfp7.quantise_fp16(x)
    q = np.asarray(q, np.int64) & 0x7F
    field = (((np.asarray(es) + mxfp7.SBIAS) & 0x1F) << 3) | (
        (np.asarray(m8) - 8) & 0x7
    )

    out = []
    for t in range(g_total // gt):
        for c in range(nk_total // bc):
            for g in range(gt):
                for b in range(bc):
                    lane0, blk = (t * gt + g) * LANES, c * bc + b
                    for w in range(4):
                        acc = 0
                        for oi in range(32):
                            lane, kk = (
                                (oi % LANES, oi // LANES)
                                if blayout
                                else (oi // 8, oi % 8)
                            )
                            k = blk * KBLOCK + w * 8 + kk
                            acc |= int(q[lane0 + lane, k]) << (249 - oi * 7)
                        for lane in range(LANES):
                            acc |= int(field[lane0 + lane, blk]) << (24 - lane * 8)
                        out.append(acc)
    return out


def entries(lanes):
    """How many L1 entries a padded operand of `lanes` lanes occupies."""
    return lanes // LANES


def unpack_c(words, m, n, gnc_cols):
    """Decode drained sub-tiles into a [M][N] float array.

    Each word is one 4x4 sub-tile of FP16; sub-tile t covers rows
    ``(t // gnc_cols)*4`` and columns ``(t % gnc_cols)*4``.
    """
    from kohakutpu.hw.mxfp7 import fp16_to_float

    out = np.zeros((m, n), dtype=np.float64)
    for t, w in enumerate(words):
        r0 = (t // gnc_cols) * 4
        c0 = (t % gnc_cols) * 4
        for i in range(4):
            for j in range(4):
                bits = (w >> ((i * 4 + j) * 16)) & 0xFFFF
                if r0 + i < m and c0 + j < n:
                    out[r0 + i, c0 + j] = fp16_to_float(bits)
    return out
