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
FP16_ENTRY_BYTES = 256  # one L1 entry of source: 4 lanes x 32 FP16
# The same entry once quantised: 4 lanes x 32 int7 + 4 E5M3 scales, packed as
# four 256-bit operand words. Half the bytes, and the form the mesh already
# carries -- which is why a pre-quantised operand costs half the fetch and none
# of the quantiser.
MXFP7_ENTRY_BYTES = 128


def entry_words(prequantised, word_bits=256):
    """Memory words one L1 entry occupies, in whichever format it is stored.

    The driver never builds MXFP7; it only needs the SIZE, because the tensor
    was converted by the hardware on its way into memory and the addresses of
    the entries after it move accordingly.
    """
    n = MXFP7_ENTRY_BYTES if prequantised else FP16_ENTRY_BYTES
    return n * 8 // word_bits


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


def entries(lanes):
    """How many L1 entries a padded operand of `lanes` lanes occupies."""
    return lanes // LANES


def unpack_c(words, m, n, gnc_cols):
    """Decode drained sub-tiles into a [M][N] float array.

    Each word is one 4x4 sub-tile of FP16; sub-tile t covers rows
    ``(t // gnc_cols)*4`` and columns ``(t % gnc_cols)*4``.
    """
    from kohakutpu.mxfp7 import fp16_to_float

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
