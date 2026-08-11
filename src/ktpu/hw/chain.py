"""Run kernels AT PLANNED ADDRESSES, so one kernel's output is the next's input.

    gemm(s, sh, a_word, b_word, c_word)         # drains where the plan said
    vmap(s, node, "silu", [c_word], h_word, n)  # reads it there, no round trip

`ktpu.hw.bench` chooses its own layout, uploads into it and reads back; that is
why chaining two matmuls costs a host round trip per link. Everything here takes
addresses as ARGUMENTS, so an intermediate no host needs never leaves the card.

A DRAIN writes one word per 4x4 sub-tile and a FILL reads tile-major 4x32
entries, so matmul into matmul needs a relayout. THE MACHINE CAN DO IT: a vector
band multiplying by 1.0 converts, and `passes.lower` inserts one automatically
for an A operand another matmul produced (docs/limits.md s6.6). This hand-built
path does not, so :func:`repack_c_to_a` crosses the link instead -- which is a
cost of THIS module, not of the hardware. B is the real restriction: only a
host-packed operand is ever in entry order.

Elementwise work needs no repack at all -- a permutation commutes with it -- so
`vmap` may read a drained C directly.
"""

from typing import ClassVar

import numpy as np

from ktpu.hw import bench, kernel, matmul, tensor
from ktpu.hw import veckernels as VK
from ktpu.hw.board import MEM_WORD_BYTES
from ktpu.hw.fpga import PREQ


class Shape:
    """A padded GEMM shape and the tile it runs with. Cached: `tile_for` runs a
    search and a chain asks for the same shape once per link."""

    _cache: ClassVar[dict] = {}

    def __new__(cls, m, k, n, caps):
        key = (m, k, n, caps)
        got = cls._cache.get(key)
        if got is None:
            got = cls._cache[key] = super().__new__(cls)
            got.tile = bench.tile_for(m, k, n, caps)
            got.m, got.k, got.n = bench.padded_shape(m, k, n, got.tile)
            got.raw = (m, k, n)
        return got

    def __repr__(self):
        return f"Shape({self.raw} -> {self.m}x{self.k}x{self.n}, {self.tile})"


def shape_of(s, m, k, n):
    return Shape(m, k, n, s.board.caps())


# ---- packing: host bytes for an address ------------------------------------
def pack_a(x, sh):
    """`x` [M,K] as the bytes a cluster FILLs for its A operand, zero padded."""
    return _pack(x, sh.m, sh.k, sh.tile.gm, sh.tile.nk)


def pack_b(bt, sh):
    """`bt` [N,K] -- B ALREADY TRANSPOSED, which is what torch stores -- as B's
    operand image."""
    return _pack(bt, sh.n, sh.k, sh.tile.gn, sh.tile.nk)


def _pack(x, rows, cols, groups, blocks):
    a = np.zeros((rows, cols), dtype=np.float32)
    src = np.asarray(x, dtype=np.float32)
    a[: src.shape[0], : src.shape[1]] = src
    words = tensor.to_fp16_words_tiled(a, groups, blocks)
    return b"".join(w.to_bytes(MEM_WORD_BYTES, "little") for w in words)


def unpack_c(raw, sh, m=None, n=None):
    """Drained sub-tile words -> a [M,N] float array, trimmed to the true shape.

    Walks tiles exactly as `bench.decode_result` does, through a stand-in
    carrying the four fields it reads -- one decoder, so a layout disagreement
    cannot hide in a second copy of the walk.
    """
    words = [int.from_bytes(raw[i: i + MEM_WORD_BYTES], "little")
             for i in range(0, len(raw), MEM_WORD_BYTES)]
    stub = _Decodable(sh)
    got = bench.decode_result(words, stub)
    return got[: m or sh.raw[0], : n or sh.raw[2]]


class _Decodable:
    """What `bench.decode_result` reads off a Problem, and nothing else."""

    def __init__(self, sh):
        self.tile, self.m, self.n = sh.tile, sh.m, sh.n
        self.layout = matmul.Layout(0, 0, 0)


def c_nbytes(sh):
    """Bytes the drain writes, rounded up to a whole burst."""
    words = (sh.m // tensor.LANES) * (sh.n // tensor.LANES)
    return -(-words // bench.DRAIN_BURST) * bench.DRAIN_BURST * MEM_WORD_BYTES


def repack_c_to_a(c, sh_next):
    """A drained C, decoded, as the next GEMM's A image. THE HOST COST OF A CHAIN.

    Nothing on the machine can do this: the two orders differ within a 256-bit
    word and every mover on the card is word-granular.
    """
    return pack_a(c, sh_next)


# ---- the cluster GEMM at an address ----------------------------------------
def gemm(s, sh, a_word, b_word, c_word, timeout=300.0, use=None):
    """`C = A @ B.T` with the operands ALREADY in memory at these words.

    Uploads nothing and reads nothing back: the caller placed A and B and the
    drain lands at `c_word`. Returns the number of rounds executed.

    Raises `HardwareError` via `Session.check` for a shape the board cannot hold.
    """
    why = s.check(*sh.raw, preq=PREQ, use=use)
    if why:
        from ktpu.hw.fpga import HardwareError

        raise HardwareError("; ".join(why))
    lay = matmul.Layout(a_word=a_word, b_word=b_word, c_word=c_word)
    kern = kernel.plan(
        sh.m, sh.k, sh.n,
        bench.dispatch_list(s.board.ncl, use, "spread", s.board.layout),
        lay, sh.tile,
        stage_flits=s.board.stage_flits, ncmd=bench.NCMD, preq=PREQ,
        l1_a=s.board.l1_a, l1_b=s.board.l1_b,
    )
    s.reset()
    for prog in kernel.programs(kern):
        prog.execute(s.link, timeout=timeout)
    return len(kern.rounds)


# ---- vector kernels at an address ------------------------------------------
def vmap(s, node, name, src_words, dst_word, nelem, timeout=300.0, chunks=8):
    """Elementwise `name` over operands at `src_words`, result at `dst_word`.

    EVERY ADDRESS IS A 256-BIT WORD, as `gemm` and `Placement.word` count them;
    the AGU descriptor wants device bytes and that conversion happens here so no
    caller holds both units. `nelem` is rounded up to a whole batch, so every
    buffer must be allocated to `pad_elems(nelem)` -- a `vmap` reads and writes
    whole batches, and on this ECC DRAM reading past the end is a bus fault.
    """
    k = VK.MapKernel(name, chunks)
    if len(src_words) != k.nin:
        raise ValueError(f"{name} takes {k.nin} inputs, got {len(src_words)}")
    nb = -(-nelem // k.batch)
    srcs = [w * MEM_WORD_BYTES for w in src_words]
    flits = VK.imem_flits(k.image) + k.static_descs()
    for b in range(nb):
        flits += k.batch_flits(srcs, dst_word * MEM_WORD_BYTES, b)
    return VK.dispatch_all(s, node, flits, timeout)


def gnorm(s, node, x_word, g_word, b_word, out_word, ngroups, nelem,
          eps=1e-5, timeout=300.0):
    """GroupNorm over `ngroups` groups of `nelem`, all four buffers in memory.

    Addresses are 256-bit words. `g_word` and `b_word` hold gamma and beta
    ALREADY BROADCAST to the group image -- the host's layout job, and the reason
    those are separate tensors in a plan rather than the rank-1 originals.

    Raises ValueError for a group L1 cannot hold: `nelem` must be a multiple of
    128 and at most 2048, which at C=320 caps the spatial extent at 204 pixels.
    """
    image, static, batch = VK.group_norm_kernel(nelem, nelem // 128, eps)
    flits = VK.imem_flits(image) + static
    for g in range(ngroups):
        flits += batch(x_word * MEM_WORD_BYTES, g_word * MEM_WORD_BYTES,
                       b_word * MEM_WORD_BYTES, out_word * MEM_WORD_BYTES, g)
    return VK.dispatch_all(s, node, flits, timeout)


def flash(s, node, sc_word, m_word, l_word, out_word, rows, cols, log2_scale,
          nrows, timeout=300.0):
    """One online-softmax step over `nrows` query rows against one key block.

    Addresses are 256-bit words. `out_word` receives p, the correction, the
    running max and the running denominator INTERLEAVED run by run, so it holds
    `4 * nrows * cols` fp16 and the caller de-interleaves -- which is also why
    this cannot chain into the next GEMM on the machine.

    Seeding `m` large-negative and `l` zero makes one call a plain softmax.

    Raises ValueError via `require_l1` for a `rows x cols` in the bad band
    (docs/limits.md s6.7), and for `nrows` not a whole number of runs.
    """
    image, static, batch = VK.flash_rows_kernel(rows, cols, log2_scale)
    if nrows % rows:
        raise ValueError(f"{nrows} query rows is not a whole number of {rows}-row runs")
    flits = VK.imem_flits(image) + static
    for i in range(nrows // rows):
        flits += batch(sc_word * MEM_WORD_BYTES, m_word * MEM_WORD_BYTES,
                       l_word * MEM_WORD_BYTES, out_word * MEM_WORD_BYTES, i)
    return VK.dispatch_all(s, node, flits, timeout)


def pad_elems(n, chunks=8):
    """`n` rounded up to a whole `vmap` batch, which is what a FLAT act must hold."""
    batch = chunks * 128
    return -(-n // batch) * batch


# ---- moving host data to and from a planned address ------------------------
def put(s, word, data):
    """Write host bytes to device word `word`. Whole 256-bit words only."""
    data = bytes(data)
    if len(data) % MEM_WORD_BYTES:
        data += bytes(MEM_WORD_BYTES - len(data) % MEM_WORD_BYTES)
    s.raw.write_block(s.board.mem_byte(word), data)
    return len(data)


def get(s, word, nbytes):
    """Read `nbytes` from device word `word`."""
    n = -(-nbytes // MEM_WORD_BYTES) * MEM_WORD_BYTES
    return s.raw.read_block(s.board.mem_byte(word), n)[:nbytes]


def put_fp16(s, word, x):
    return put(s, word, np.ascontiguousarray(x, dtype=np.float16).tobytes())


def get_fp16(s, word, n, shape=None):
    raw = get(s, word, n * 2)
    out = np.frombuffer(raw, dtype=np.float16)[:n]
    return out.reshape(shape) if shape else out


def fill(s, word, nbytes, pattern=b"\xad\xde"):
    """Write a marker over a buffer. ECC DRAM faults on a never-written line, so
    a buffer a kernel might not reach must be written before it is read."""
    n = -(-nbytes // MEM_WORD_BYTES) * MEM_WORD_BYTES
    s.raw.write_block(s.board.mem_byte(word), pattern * (n // len(pattern)))
    return n
