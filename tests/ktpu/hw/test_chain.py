"""Kernels at planned addresses: does the operand actually land where asked?

Run against the same fake machine as `test_fpga.py`, so no card is needed. No
arithmetic happens there, which is exactly right for these questions -- every
one of them is about ADDRESSES, and an address that is wrong by a tile is the
failure this module exists to make impossible.
"""

import numpy as np
import pytest
from test_fpga import FakeMachine

from ktpu.hw import bench, chain, fpga, tensor
from ktpu.hw import board as bd
from ktpu.hw import device as dev
from ktpu.hw.board import MEM_WORD_BYTES

BOARD = "singlemesh_2x2"
M, K, N = 16, 64, 16


def session(**kw):
    board = bd.Board.named(BOARD)
    machine = FakeMachine(board, **kw)
    return fpga.Session(board=board, transport=machine, timeout=2.0), machine


def staged_flits(machine):
    """Every instruction flit the driver wrote into the staging RAM, in order."""
    out, slot = [], 0
    while True:
        flit = 0
        seen = False
        for w in range(dev.FLIT_WORDS):
            a = machine.board.ctrl(dev.A_STAGE + (slot * dev.FLIT_WORDS + w) * 8)
            if a in machine.mem:
                seen = True
            flit |= machine.mem.get(a, 0) << (w * 64)
        if not seen:
            return out
        out.append(flit)
        slot += 1


def opfields(flit):
    """`(op, addr, n)` out of a CU_INST payload, as `matmul._flit` packs them."""
    p = flit & ((1 << 256) - 1)
    return (p >> 252) & 0xF, (p >> 218) & ((1 << 34) - 1), (p >> 202) & 0xFFFF


# ---- the shape --------------------------------------------------------------
def test_a_shape_pads_up_to_whole_tiles_and_is_cached():
    s, _ = session()
    a = chain.shape_of(s, M, K, N)
    assert a is chain.shape_of(s, M, K, N)
    assert a.m % a.tile.m == 0 and a.k % a.tile.k == 0 and a.n % a.tile.n == 0
    assert (a.m, a.k, a.n) >= (M, K, N)


# ---- the addresses actually reach the flits ---------------------------------
@pytest.mark.parametrize("a_word,b_word,c_word", [
    (0, 64, 128), (8192, 1024, 40960), (1 << 20, (1 << 20) + 512, 1 << 21),
])
def test_the_fills_and_the_drain_carry_the_words_they_were_given(
        a_word, b_word, c_word):
    """A cluster addresses memory in BYTES, so every one of these is word*32."""
    s, m = session()
    sh = chain.shape_of(s, M, K, N)
    chain.gemm(s, sh, a_word, b_word, c_word, timeout=2.0)

    ops = [opfields(f) for f in staged_flits(m)]
    fills = [a for op, a, _ in ops if op == 1]
    drains = [a for op, a, _ in ops if op == 3]
    assert a_word * MEM_WORD_BYTES in fills
    assert b_word * MEM_WORD_BYTES in fills
    assert drains and all(d == c_word * MEM_WORD_BYTES for d in drains)


def test_moving_the_operands_moves_nothing_but_the_addresses():
    """Relocation must change addresses and NOT the arithmetic: same shape, same
    op sequence, same entry counts."""
    s, m1 = session()
    sh = chain.shape_of(s, M, K, N)
    chain.gemm(s, sh, 0, 64, 128, timeout=2.0)
    first = [(op, n) for op, _, n in map(opfields, staged_flits(m1))]

    s2, m2 = session()
    chain.gemm(s2, sh, 4096, 8192, 16384, timeout=2.0)
    second = [(op, n) for op, _, n in map(opfields, staged_flits(m2))]
    assert first == second


def test_the_relocated_program_matches_the_planner_at_its_own_layout():
    """At bench's own layout the two paths must emit byte-identical flits, or
    `chain` is a second planner that will drift from the first."""
    s, m = session()
    sh = chain.shape_of(s, M, K, N)
    lay, _ = bench.plan(sh.m, sh.k, sh.n, sh.tile, fpga.PREQ)
    chain.gemm(s, sh, lay.a_word, lay.b_word, lay.c_word, timeout=2.0)
    mine = staged_flits(m)

    prob = bench.build(M, K, N, ncl=s.board.ncl, preq=fpga.PREQ,
                       mesh_layout=s.board.layout, caps=s.board.caps())
    theirs = [f for p in prob.kern.passes for f in p.flits]
    assert mine == theirs


def test_a_shape_the_board_cannot_hold_is_refused_before_anything_is_staged():
    s, m = session()
    sh = chain.shape_of(s, M, K, N)
    with pytest.raises(fpga.HardwareError):
        chain.gemm(s, sh, 0, 64, 128, timeout=2.0, use=99)
    assert staged_flits(m) == []


# ---- packing agrees with the one authoritative packer -----------------------
def test_pack_a_is_the_same_bytes_bench_would_have_uploaded():
    """Two packers would be two descriptions of the entry order; there is one."""
    s, _ = session()
    sh = chain.shape_of(s, M, K, N)
    rng = np.random.default_rng(5)
    x = rng.normal(0, 1, (M, K)).astype(np.float32)

    prob = bench.build(M, K, N, ncl=s.board.ncl, preq=fpga.PREQ,
                       mesh_layout=s.board.layout, caps=s.board.caps())
    prob.a[:] = 0.0
    prob.a[:M, :K] = x
    words, regions = bench.upload(prob)
    want = b"".join(w.to_bytes(MEM_WORD_BYTES, "little")
                    for w in words[: regions[0][2]])
    assert chain.pack_a(x, sh) == want


def test_pack_b_takes_b_already_transposed():
    s, _ = session()
    sh = chain.shape_of(s, M, K, N)
    rng = np.random.default_rng(6)
    bt = rng.normal(0, 1, (N, K)).astype(np.float32)

    prob = bench.build(M, K, N, ncl=s.board.ncl, preq=fpga.PREQ,
                       mesh_layout=s.board.layout, caps=s.board.caps())
    prob.bt[:] = 0.0
    prob.bt[:N, :K] = bt
    words, regions = bench.upload(prob)
    off = regions[1][0]
    want = b"".join(w.to_bytes(MEM_WORD_BYTES, "little")
                    for w in words[off: off + regions[1][2]])
    assert chain.pack_b(bt, sh) == want


def test_a_shorter_operand_is_zero_padded_rather_than_refused():
    s, _ = session()
    sh = chain.shape_of(s, M, K, N)
    small = np.ones((4, 32), dtype=np.float32)
    assert len(chain.pack_a(small, sh)) == len(chain.pack_a(
        np.ones((M, K), dtype=np.float32), sh))


# ---- the drain, decoded -----------------------------------------------------
def test_unpack_c_inverts_the_drain_order_for_a_known_image():
    """Build the sub-tile words a drain would write, then read them back."""
    s, _ = session()
    sh = chain.shape_of(s, M, K, N)
    rng = np.random.default_rng(9)
    c = rng.normal(0, 1, (sh.m, sh.n)).astype(np.float16)

    t = sh.tile
    n_tiles = sh.n // t.n
    words = [0] * ((sh.m // tensor.LANES) * (sh.n // tensor.LANES))
    for mo in range(sh.m // t.m):
        for no in range(n_tiles):
            base = (mo * n_tiles + no) * t.gm * t.gn
            for st in range(t.gm * t.gn):
                g, h = divmod(st, t.gn)
                w = 0
                for i in range(tensor.LANES):
                    for j in range(tensor.LANES):
                        r = mo * t.m + g * tensor.LANES + i
                        col = no * t.n + h * tensor.LANES + j
                        bits = int(c[r, col].view(np.uint16))
                        w |= bits << ((i * tensor.LANES + j) * 16)
                words[base + st] = w
    raw = b"".join(w.to_bytes(MEM_WORD_BYTES, "little") for w in words)
    got = chain.unpack_c(raw, sh)
    assert np.allclose(got, c[:M, :N].astype(np.float64))


def test_c_nbytes_covers_the_whole_drain_and_is_burst_rounded():
    s, _ = session()
    sh = chain.shape_of(s, M, K, N)
    sub = (sh.m // tensor.LANES) * (sh.n // tensor.LANES)
    nb = chain.c_nbytes(sh)
    assert nb >= sub * MEM_WORD_BYTES
    assert nb % (bench.DRAIN_BURST * MEM_WORD_BYTES) == 0


# ---- the vector side --------------------------------------------------------
def descriptors(machine):
    """`(ad, fld) -> value` for every descriptor write staged, last one wins."""
    out = {}
    for f in staged_flits(machine):
        p = f & ((1 << 256) - 1)
        if (p >> 252) & 0xF != 2:
            continue
        out[((p >> 249) & 0x7, (p >> 246) & 0x7)] = (p >> 212) & ((1 << 34) - 1)
    return out


def test_vmap_points_its_fill_and_drain_at_the_words_it_was_given():
    """Addresses are WORDS everywhere in `chain`; the AGU wants device bytes and
    the factor of 32 is applied in exactly one place."""
    s, m = session()
    node = tuple(s.board.vectors[0])
    src, dst = 0x20000, 0x40000
    k = chain.VK.MapKernel("silu", 8)
    chain.vmap(s, node, "silu", [src], dst, k.batch, timeout=2.0)

    d = descriptors(m)
    assert d[(k.ad_fill[0], 0)] == src * MEM_WORD_BYTES
    assert d[(k.ad_drain, 0)] == dst * MEM_WORD_BYTES


def test_a_second_batch_advances_both_bases_by_one_batch_of_bytes():
    s, m = session()
    node = tuple(s.board.vectors[0])
    k = chain.VK.MapKernel("silu", 8)
    src, dst = 0x20000, 0x40000
    chain.vmap(s, node, "silu", [src], dst, 2 * k.batch, timeout=2.0)

    d = descriptors(m)
    assert d[(k.ad_fill[0], 0)] == src * MEM_WORD_BYTES + k.batch * 2
    assert d[(k.ad_drain, 0)] == dst * MEM_WORD_BYTES + k.batch * 2


def test_gnorm_points_all_four_buffers_where_it_was_told():
    s, m = session()
    node = tuple(s.board.vectors[0])
    words = (0x8000, 0x10000, 0x18000, 0x20000)
    chain.gnorm(s, node, *words, ngroups=1, nelem=256, timeout=2.0)
    d = descriptors(m)
    assert {d[(0, 0)], d[(1, 0)], d[(2, 0)], d[(3, 0)]} == {
        w * MEM_WORD_BYTES for w in words}


def test_gnorm_refuses_a_group_wider_than_vector_l1():
    """At C=320 this is what caps the spatial extent at 204 pixels."""
    s, _ = session()
    node = tuple(s.board.vectors[0])
    with pytest.raises(ValueError, match="L1 has 512"):
        chain.gnorm(s, node, 0, 8, 16, 24, ngroups=1, nelem=4096, timeout=2.0)


def test_vmap_refuses_the_wrong_number_of_inputs():
    s, _ = session()
    node = tuple(s.board.vectors[0])
    with pytest.raises(ValueError, match="takes 2 inputs"):
        chain.vmap(s, node, "add", [0x1000], 0x2000, 1024, timeout=2.0)


def test_pad_elems_rounds_up_to_a_whole_vmap_batch():
    assert chain.pad_elems(1) == 1024
    assert chain.pad_elems(1024) == 1024
    assert chain.pad_elems(1025) == 2048


# ---- host transfers land at the planned word --------------------------------
def test_put_and_get_use_the_boards_memory_map():
    s, m = session()
    x = np.arange(16, dtype=np.float16)
    chain.put_fp16(s, 64, x)
    assert m.board.mem_byte(64) in m.mem
    assert np.array_equal(chain.get_fp16(s, 64, 16), x)


def test_put_pads_a_partial_word_rather_than_writing_a_short_block():
    s, _ = session()
    n = chain.put(s, 8, b"\x01\x02\x03")
    assert n == MEM_WORD_BYTES
