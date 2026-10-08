"""Pre-quantised cluster operands: what a FILL reads on the RTL.

`mx_cluster_cu.v` fills MXFP7 entries (128 B, no conversion on the way in),
made on the card by the mover's quantiser from FP16 the host uploaded. The
decoder is witnessed against tests/rv64/matmul_artifact.h -- operands and a
golden result the RTL reproduces when the RV64 node replays it -- and the
model's mover-converted matmul against the sweep on the host's numbers.
"""

import pathlib
import re
import struct

import numpy as np
import pytest
from kohakutpu.hw import tensor as T
from kohakutpu.model import SimDevice, sweep, sweep_q

from kohakutpu import layout as LO
from kohakutpu import ops

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _golden():
    h = (ROOT / "tests/rv64/matmul_artifact.h").read_text()

    def arr(name):
        body = re.search(name + r"\[\d+\] = \{([^}]*)\}", h).group(1)
        return [int(x, 16) for x in body.split(",")]

    return arr("MM_A"), arr("MM_B"), arr("MM_C_GOLD")


def test_decoder_reproduces_the_rtl_golden_with_b_transposed():
    a, b, gold = _golden()

    def one(raw, blayout):
        q, es, m8 = T.from_mxfp7_entries(struct.pack("<64H", *raw), blayout)
        return q[0], es[0][:, None], m8[0][:, None]

    want = np.array(gold, np.uint16).view(np.float16).reshape(4, 4)
    got = sweep_q(one(a, 0), one(b, 1)).astype(np.float16)
    assert np.array_equal(got, want)
    wrong = sweep_q(one(a, 0), one(b, 0)).astype(np.float16)
    assert not np.array_equal(wrong, want), "the B slot map made no difference"


def test_mxentry_packs_half_the_bytes_and_reads_back_quantised():
    rng = np.random.default_rng(1)
    x = (rng.standard_normal((16, 64)) * 3).astype(np.float16)
    lay = LO.MxEntry(2, 2, 0)
    raw = lay.pack(x)
    assert len(raw) == lay.nbytes(x.shape) == LO.Entry(2, 2).nbytes(x.shape) // 2
    back = lay.unpack(raw, x.shape)
    assert np.abs(back - x).max() <= np.abs(x).max() / 32


@pytest.mark.parametrize(
    "shape,knobs",
    [
        ((32, 64, 32), {}),
        ((16, 64, 16), dict(gm=2, gn=2, nk=1)),
        ((64, 128, 32), dict(gm=4, gn=2, nk=2)),
    ],
)
def test_every_operand_is_quantised_by_the_mover(shape, knobs):
    """Both operands go up FP16 and are quantised by the mover (`run_move`).

    The MXFP7 copies on the card must equal the reference packer's bytes.
    """
    m, k, n = shape
    rng = np.random.default_rng(7)
    a = (rng.standard_normal((m, k)) * 0.5).astype(np.float16)
    w = (rng.standard_normal((n, k)) * 0.5).astype(np.float16)
    dev = SimDevice(mg=((1, 1),), vc=((1, 0),), agent=(0, 1))
    ta, tw = dev.tensor(a), dev.tensor(w)
    got = ops.matmul(ta, tw, **knobs).numpy()
    assert dev.counters.get("quantised") == 2 and dev.counters.get("moves") == 2
    for t, x in ((ta, a), (tw, w)):
        (mx,) = [b for b in t.buffers.values() if isinstance(b.layout, LO.MxEntry)]
        assert dev.read(mx.addr, mx.nbytes) == mx.layout.pack(x)
    ref = a.astype(np.float64) @ w.astype(np.float64).T
    assert np.abs(got - ref).max() <= 0.05 * np.abs(ref).max()
    assert np.abs(got - sweep(a, w)).max() <= np.abs(ref).max() / 512


def _pair(seed=9):
    rng = np.random.default_rng(seed)
    a = (rng.standard_normal((32, 64)) * 0.5).astype(np.float16)
    w = (rng.standard_normal((32, 64)) * 0.5).astype(np.float16)
    return a, w


def test_an_operand_uploaded_in_mxfp7_is_filled_with_no_move():
    """The format is the operand's own: packed on the host, nothing converts it."""
    a, w = _pair()
    dev = SimDevice(mg=((1, 1),), vc=((1, 0),), agent=(0, 1))
    ta, tw = dev.tensor(a), dev.tensor(w)
    tw.upload(ops.matmul.plan(ta, tw).layouts["b"])
    got = ops.matmul(ta, tw).numpy()
    assert dev.counters.get("quantised") == 1 and dev.counters.get("moves") == 1
    plain = SimDevice(mg=((1, 1),), vc=((1, 0),), agent=(0, 1))
    assert np.array_equal(got, ops.matmul(plain.tensor(a), plain.tensor(w)).numpy())


def test_a_weight_reused_across_calls_is_converted_once():
    a, w = _pair()
    a2, _ = _pair(10)
    dev = SimDevice(mg=((1, 1),), vc=((1, 0),), agent=(0, 1))
    tw = dev.tensor(w)
    ops.matmul(dev.tensor(a), tw).numpy()
    assert dev.counters.get("quantised") == 2
    ops.matmul(dev.tensor(a2), tw).numpy()
    assert dev.counters.get("quantised") == 3, "the weight was converted again"
