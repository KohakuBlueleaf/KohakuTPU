"""The behaviour model's units on their own: a vector core running images
assembled here (`compiler.encode.vector`), a cluster running FILL / GEMM /
DRAIN words (`compiler.isa.cluster`), each against numpy and the MXFP7
reference (`language.numerics.mxfp7`), and the two talking through a card
(credits, a COUNT burst)."""

import numpy as np
import pytest

from kohakutpu.compiler.encode import vector as A
from kohakutpu.compiler.isa import core as C
from kohakutpu.compiler.isa.cluster import ISA as CU
from kohakutpu.compiler.isa.vector import ISA as VEC
from kohakutpu.compiler.layout import tensor
from kohakutpu.language.numerics import mxfp7
from kohakutpu.simulation.behave import cluster as BC
from kohakutpu.simulation.behave import vector as BV
from kohakutpu.simulation.behave.card import Card


class _Mem:
    def __init__(self, n: int = 1 << 20) -> None:
        self.b = bytearray(n)

    def read_block(self, addr: int, n: int) -> bytes:
        return bytes(self.b[addr : addr + n])

    def write_block(self, addr: int, data: bytes) -> None:
        self.b[addr : addr + len(data)] = data


class _Net:
    """A card that only records what a unit sends."""

    def __init__(self) -> None:
        self.sent: list = []

    def credit(self, mesh, to, n) -> None:
        self.sent.append(("credit", to, n))

    def burst(self, mesh, to, buf, off, data, **kw) -> None:
        self.sent.append(("burst", to, buf, off, data))


def _image(core, words, descs=()) -> None:
    """Load `words` (a VSETI's immediate as its own word) and run them."""
    for (ad, fld), value in descs:
        core.deliver(VEC.desc_base(ad, value) if fld == 0 else VEC.desc(ad, fld, value))
    for i, w in enumerate([*words, A.vhalt()]):
        core.deliver(VEC.imem(i, w))
    core.deliver(VEC.run(0))
    while core.step():
        pass
    assert core.task is None and not core.words


def _fill_drain(mem, x, rows, *body):
    """x [rows, 16] f16 at 0 into L1 0.., `body`, L1 64.. back to 4096."""
    mem.write_block(0, x.astype(np.float16).tobytes())
    descs = [
        ((0, 0), 0),
        ((0, 1), A.dim(32, rows)),
        ((1, 0), 4096),
        ((1, 1), A.dim(32, rows)),
    ]
    return [A.vfill(0, 0), *body, A.vdrain(1, 64)], descs


def test_a_vector_fma_of_a_constant_on_eight_chunks():
    mem, core = _Mem(), BV.VectorCore(_Net(), (0, 0, 0), _Mem())
    core.mem = mem
    x = np.arange(128, dtype=np.float16).reshape(8, 16) / 8
    words, descs = _fill_drain(
        mem,
        x,
        8,
        A.vunpk(0, 0),
        A.vseti(2),
        A.e8m15(0.5),
        A.math("vfma", 1, 0, 2, 0, sb=A.SRC_S, sc=A.SRC_K),  # x * s2 + k1
        A.vpack(1, 64),
    )
    # vfma reads c = K[0 & 3] = 0.0 here: x * 0.5
    _image(core, words, descs)
    got = np.frombuffer(mem.read_block(4096, 256), np.float16).reshape(8, 16)
    assert np.array_equal(got, (x * 0.5).astype(np.float16))


def test_vector_compare_select_and_crossbar():
    """p1 = x > 3; out = sel(c, a, b) under the predicate; b through xor 1."""
    mem, core = _Mem(), BV.VectorCore(_Net(), (0, 0, 0), _Mem())
    core.mem = mem
    x = np.arange(16, dtype=np.float16).reshape(1, 16)
    words, descs = _fill_drain(
        mem,
        x,
        1,
        A.vunpk(0, 0, n=1),
        A.chkvl(16),
        A.xb(pr=1),
        A.math("vcmpgt", 1, 0, 1, sb=A.SRC_K, om=True),  # x > k1 (1.0) -> p1
        A.math("vmov", 2, 0, om=False),
        A.xb(xm=A.XM_XOR, xk=1, xc=True, pm=1, pr=1),
        A.math(
            "vadd", 2, 0, 0, 0, om=True
        ),  # x + xor1(x) (c: b's crossbar) where x > 1
        A.vpack(2, 64, n=1),
    )
    _image(core, words, descs)
    got = np.frombuffer(mem.read_block(4096, 32), np.float16)
    lanes = np.arange(16)
    v = x[0].astype(np.float64)
    want = np.where(v > 1, v + v[lanes ^ 1], v)
    assert np.array_equal(got, want.astype(np.float16))


def test_a_vwait_blocks_until_its_granules_land():
    core = BV.VectorCore(_Net(), (0, 0, 0), _Mem())
    core.deliver(VEC.imem(0, A.vwait(A.W_FE, C.RX, 2)))
    core.deliver(VEC.imem(1, A.vhalt()))
    core.deliver(VEC.run(0))
    while core.step():
        pass
    assert core.task is not None  # waiting
    core.land(0, 10, bytes(64), count=True)
    assert core.step() and core.task is None


@pytest.mark.parametrize("blayout", [False, True])
def test_an_entry_decodes_to_the_quantised_values(blayout):
    """`entry_values` against `mxfp7.value_fp16`, an independent reference."""
    x = (np.random.default_rng(1).standard_normal((4, 32)) * 3).astype(np.float16)
    raw = tensor.pack(x, 1, 1, int(blayout))
    assert np.array_equal(BC.entry_values(raw, blayout), mxfp7.value_fp16(x))


def test_a_cluster_sweep_is_the_mxfp7_product():
    """[8, 64] x [8, 64]^T: two sub-tile rows, two columns, two K-blocks."""
    rng = np.random.default_rng(2)
    a = rng.standard_normal((8, 64)).astype(np.float16)
    b = rng.standard_normal((8, 64)).astype(np.float16)
    mem = _Mem()
    mem.write_block(0, tensor.pack_a(a, 2, 2))
    mem.write_block(8192, tensor.pack_b(b, 2, 2))
    cl = BC.Cluster(_Net(), (0, 1, 0), mem)
    for w in (
        CU.fill(0, 4, sel=0),
        CU.fill(8192, 4, sel=1),
        CU.gemm(2, 2, 2),
        CU.drain(16384, 4),
    ):
        cl.deliver(w)
    while cl.step():
        pass
    tiles = np.frombuffer(mem.read_block(16384, 128), np.float16).reshape(2, 2, 4, 4)
    got = tiles.transpose(0, 2, 1, 3).reshape(8, 8).astype(np.float64)
    want = mxfp7.model_matmul(a, b)
    assert np.allclose(got, want, rtol=2e-3, atol=2e-3)
    assert len(cl.out) == 4


def test_a_credited_emit_waits_for_its_consumer_on_a_card():
    """A cluster emits 4 sub-tiles into a core's L1 on credits: none leave
    before the core's VPOST, then all land, counted."""
    mem = _Mem()
    card = Card(mem)
    cl = BC.Cluster(card, (0, 1, 0), mem)
    core = BV.VectorCore(card, (0, 2, 0), mem)
    card.units = {(0, 1, 0): cl, (0, 2, 0): core}
    cl.acc = {t: np.full((4, 4), float(t)) for t in range(4)}
    cl.deliver(
        CU.DRAIN.encode(n=4, dnode=1, dst_x=2, dst_y=0, dbuf=0, dflags=2, credit=1)
    )
    card.settle()
    assert core.count[C.RX] == 0 and cl.queue
    core.deliver(VEC.imem(0, A.vpost((1, 0), 4)))
    core.deliver(VEC.imem(1, A.vwait(A.W_FE, C.RX, 4)))
    core.deliver(VEC.imem(2, A.vhalt()))
    core.deliver(VEC.run(0))
    card.settle()
    assert core.count[C.RX] == 4 and not cl.queue and core.task is None
    got = np.frombuffer(bytes(core.l1[: 4 * 32]), np.float16).reshape(4, 16)
    assert np.array_equal(got[:, 0], np.arange(4, dtype=np.float16))
