"""All-zero MXFP7 blocks in K pairs on the card model (NUM.MX7.008,
NUM.ACC.008, RUL.099).

`mx_acu_fp_pump.v` merges a K pair's partials at the larger exponent sum with
no zero test, so an all-zero block with the unit scale (E=20) truncates its
live partner. Every quant path feeding an nk >= 2 contraction gives the block
E=0 by quantising zeros as `mxfp7.ZERO_LIFT`.

Bytes: on each path, into nk=2 entries the lift changes exactly the scale
fields of the all-zero blocks (0xA0 to 0x01), nothing else, for -0, subnormal,
NaN and infinite inputs too where the path takes them, and into nk=1 entries
nothing; the device, the
host packer and the Python packer hash equal.
Numbers: on LeNet conv1 dW and causal P @ V, each nk=2 path's error stays at
the unpaired (nk=1) product's, and its unit-scale mutant's does not.
"""

import hashlib
import json

import numpy as np
import pytest

from application.tests.runtime import zero_block as Z
from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.language.numerics import mxfp7
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs

BOARD = "vlt_v9t6s_1n"
#: Each lifting path's mutant: the path quantising zeros as they are.
UNLIFTED = {
    "host": "num.mx7/unit-scale-zero",
    "vector_quant": "l2.packing/unit-scale-zero",
    "fused_pack": "l2.vector/unit-scale-zero",
}
#: (rows, cols, side, groups): A and B packings, one to several tiles and
#: K pairs.
SHAPES = [(8, 64, "a", 2), (16, 128, "b", 4), (64, 256, "a", 8), (32, 192, "b", 8)]


def sha(b):
    return hashlib.sha256(b).hexdigest()


def sweep(rows, cols, seed, specials):
    """FP16 data over every finite class (both zeros, subnormals, normals up
    to 65504), all-zero blocks of mixed +0 / -0, and with `specials` some
    NaN and infinite elements."""
    rng = np.random.default_rng(seed)
    bits = rng.integers(0, 0x7C00, (rows, cols), dtype=np.uint16)
    bits |= rng.integers(0, 2, (rows, cols), dtype=np.uint16) << 15
    small = rng.random((rows, cols)) < 0.3
    bits[small] = (bits[small] & 0x83FF) | 0x0400 * rng.integers(0, 3, small.sum())
    bits[rng.random((rows, cols)) < 0.1] &= 0x8000
    blocks = bits.reshape(rows, -1, 32)
    zero = rng.random(blocks.shape[:2]) < 0.35
    zero[0, 0] = zero[-1, -1] = True
    blocks[zero] = rng.integers(0, 2, (zero.sum(), 32), dtype=np.uint16) << 15
    if specials:
        live = ~zero.repeat(32, 1).reshape(rows, cols)
        pick = live & (rng.random((rows, cols)) < 0.01)
        bits[pick] = rng.choice([0x7C00, 0xFC00, 0x7E00, 0x7C01, 0xFFFF], pick.sum())
    return bits.view(np.float16)


def packed(dev, path, x, side, groups, nk, mutant=None):
    """`x` packed on `path` into entries of `nk` K-blocks."""
    if path == "host":
        return Z.host(x, side, groups, nk, rs.machine_json(dev.machine), mutant)
    if path == "vector_quant":
        return Z.quant(dev, x, side, groups, nk, mutant)[0]
    return Z.fused(dev, x, side, groups, nk, mutant)[0]


@pytest.fixture
def vector_dev(dev, monkeypatch):
    monkeypatch.setattr(dev, "machine", machine_of(BOARD, packing_backend="vector_mx7"))
    return dev


#: (path, specials): the generated quant's `finite=True` excludes NaN / Inf
#: input; its checked form refusing it, lifted, is test_packing_checked_model's.
SWEEPS = [
    (p, s) for p in UNLIFTED for s in (False, True) if not (s and p == "vector_quant")
]


@pytest.mark.parametrize("path,specials", SWEEPS)
@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("nk", [1, 2])
def test_lift_changes_only_zero_block_fields(vector_dev, path, specials, shape, nk):
    """In K pairs (nk=2) the lift changes exactly the zero blocks' fields;
    unpaired (nk=1) it changes nothing."""
    rows, cols, side, groups = shape
    x = sweep(rows, cols, 61 + rows + cols, specials)
    got = packed(vector_dev, path, x, side, groups, nk)
    base = packed(vector_dev, path, x, side, groups, nk, UNLIFTED[path])
    # The Python packers lifted and not: they differ in the zero blocks' fields.
    lifted = Z.reference(mxfp7.lift(x, 2), side, groups, nk)
    plain = Z.reference(x, side, groups, nk)
    want = {i for i in range(len(plain)) if plain[i] != lifted[i]}
    zero = (x.view(np.uint16).reshape(rows, -1, 32) & 0x7FFF).max(-1) == 0
    assert len(want) == 4 * int(zero.sum()) > 0
    assert {plain[i] for i in want} == {0xA0} and {lifted[i] for i in want} == {0x01}
    diff = {i for i in range(len(got)) if got[i] != base[i]}
    assert diff == (want if nk == 2 else set())
    assert {got[i] for i in diff} <= {0x01} and {base[i] for i in diff} <= {0xA0}
    if not specials:
        assert sha(got) == sha(lifted if nk == 2 else plain)
        assert sha(base) == sha(plain)


@pytest.mark.parametrize("shape", SHAPES)
def test_device_host_and_python_packings_hash_equal(vector_dev, shape):
    rows, cols, side, groups = shape
    x = sweep(rows, cols, 7 + rows, False)
    want = sha(Z.reference(mxfp7.lift(x, 2), side, groups, 2))
    assert sha(packed(vector_dev, "vector_quant", x, side, groups, 2)) == want
    assert sha(packed(vector_dev, "host", x, side, groups, 2)) == want


def test_checked_quant_lifts_without_a_nonfinite_status(vector_dev):
    """The checked classifier sees the lifted 2^-24, which is finite: a block
    of zeros packs E=0 and the package completes."""
    x = sweep(16, 128, 5, False)
    text = (
        "target ktpu.v9\n\n"
        f"fn zq.l2(x: f16[16, 128] @dram, q: mx7a[16, 128] @dram({Z.layout('a', 4, 2)}))\n"
        "    quant q <- x\n"
    )
    c = K.compile(text, machine=vector_dev.machine)
    assert "assert_zero" in c.l1_text
    xa, qa = vector_dev.alloc(x.nbytes), vector_dev.alloc(x.size)
    try:
        vector_dev.write(xa, x.tobytes())
        vector_dev.run(c, {"x": xa, "q": qa})
        got = vector_dev.read(qa, x.size)
    finally:
        vector_dev.free(xa)
        vector_dev.free(qa)
    assert sha(got) == sha(Z.reference(mxfp7.lift(x, 2), "a", 4, 2))


@pytest.mark.parametrize("side", ["a", "b"])
@pytest.mark.parametrize("rows,rp", [(16, 16), (12, 16)])
def test_a_k_tail_packs_as_its_lifted_zero_padding(vector_dev, side, rows, rp):
    """A 96-deep K paired into 128 (pad_k): the generated quant of the 96
    source columns packs as the lifted zero-padded [rp, 128] packs, and
    under the unit-scale mutant does not."""
    x = sweep(rows, 96, 11 + rows, False)
    want = Z.reference(mxfp7.lift(Z.padded(x, rp, 128), 2), side, rp // 4, 2)
    got = Z.quant(vector_dev, x, side, rp // 4, 2, padded_to=(rp, 128))[0]
    assert sha(got) == sha(want)
    unlifted = Z.quant(
        vector_dev,
        x,
        side,
        rp // 4,
        2,
        "l2.packing/unit-scale-zero",
        padded_to=(rp, 128),
    )[0]
    assert sha(unlifted) != sha(want)


@pytest.fixture(scope="module")
def contractions():
    return {
        "lenet_conv1_dw": Z.lenet_relu_step(Z.mnist8())["lenet_conv1_dw"],
        "causal_av": Z.causal_av(),
    }


@pytest.fixture(scope="module")
def unpaired(card_device, contractions):
    """Each contraction's error with nk=1 (no pairing), mover QUANT."""
    dev = card_device
    saved = dev.machine
    dev.machine = machine_of(BOARD, packing_backend="mover_quant")
    mt = rs.machine_json(dev.machine)
    out = {}
    try:
        for name, (a0, b0) in contractions.items():
            mm, a, b = Z.operands(a0, b0, mt, "unpaired")
            assert mm.nk == 1
            source = Z.mm_source(mm, "unpaired", mt)
            qa = Z.quant(dev, a, "a", mm.tm // 4, 1)[0]
            qb = Z.quant(dev, b, "b", mm.tn // 4, 1)[0]
            got, _ = Z.product(dev, mm, source, qa, qb)
            out[name] = Z.error(got, a, b, a0.shape[0], b0.shape[0])
    finally:
        dev.machine = saved
    return out


#: nk=2 operand paths on the mover-preferring board, each bare and under the
#: mutant that restores the unit-scale zero block: host-packed operands, the
#: generated quant (vector_mx7 by L2.MOV.018), and that quant lowered through
#: mover QUANT once the L2.MOV.018 routing and refusal are off.
PAIRED = [
    ("host", None),
    ("host", "num.mx7/unit-scale-zero"),
    ("quant", None),
    ("quant", "l2.packing/unit-scale-zero"),
    ("quant", "l2.packing/mover-paired"),
]


@pytest.mark.parametrize("name", ["lenet_conv1_dw", "causal_av"])
@pytest.mark.parametrize("path,mutant", PAIRED)
def test_paired_error_stays_at_the_unpaired_error(
    dev, monkeypatch, contractions, unpaired, name, path, mutant
):
    monkeypatch.setattr(
        dev, "machine", machine_of(BOARD, packing_backend="mover_quant")
    )
    mt = rs.machine_json(dev.machine)
    a0, b0 = contractions[name]
    mm, a, b = Z.operands(a0, b0, mt, "vector")
    assert mm.nk == 2
    ga, gb = mm.tm // 4, mm.tn // 4
    if path == "host":
        qa, qb = Z.host(a, "a", ga, 2, mt, mutant), Z.host(b, "b", gb, 2, mt, mutant)
    else:
        qa = Z.quant(dev, a, "a", ga, 2, mutant)[0]
        qb = Z.quant(dev, b, "b", gb, 2, mutant)[0]
    got, _ = Z.product(dev, mm, Z.mm_source(mm, "vector", mt), qa, qb)
    err = Z.error(got, a, b, a0.shape[0], b0.shape[0])
    base = unpaired[name]
    print(
        json.dumps({"name": name, "path": path, "mutant": mutant, **err, "nk1": base})
    )
    if mutant:
        assert err["mean_abs"] > 3 * base["mean_abs"]
    else:
        assert err["mean_abs"] <= 1.5 * base["mean_abs"]
        assert err["max_rel_of_peak"] <= 1.5 * base["max_rel_of_peak"]


@pytest.mark.parametrize("backend", ["mover_quant", "vector_mx7"])
@pytest.mark.parametrize("pairing", ["vector", "unpaired"])
def test_python_plan_pairs_as_the_rust_plan(backend, pairing):
    """`matmul.plan` (Python, the pairing rule read through `_rs`) and
    ktpu-kernels `plan` give one nk on a K of whole block pairs; under
    `machine.pairing/mover-paired` the Rust plan pairs a mover-quantised
    contraction and the two disagree."""
    mt = rs.machine_json(machine_of(BOARD, packing_backend=backend))
    mm = MM.plan(1, 64, 64, 128, pairing=pairing, machine_json=mt)
    assert mm.nk == (1 if (backend, pairing) == ("mover_quant", "unpaired") else 2)
    assert Z.port_nk(Z.mm_source(mm, pairing, mt), "a") == mm.nk
    mutated = Z.port_nk(
        Z.mm_source(mm, pairing, mt, "machine.pairing/mover-paired"), "a"
    )
    assert (mutated == mm.nk) == (mm.nk == 2)


def test_zero_lift_is_one_value():
    """Python's `mxfp7.ZERO_LIFT` is the value ktpu-l1 packs a zero as: an
    all-zero block packs as its Python-lifted copy, with the E=0 field the
    Python quantiser gives the lifted block."""
    mt = rs.machine_json(machine_of(BOARD))
    x = np.zeros((8, 64), np.float16)
    x[:, 32:] = 1
    got = Z.host(x, "a", 2, 2, mt)
    assert got == Z.host(mxfp7.lift(x, 2), "a", 2, 2, mt)
    assert sha(got) == sha(Z.reference(mxfp7.lift(x, 2), "a", 2, 2))
    _, es, m8 = mxfp7.quantise_fp16(mxfp7.lift(x, 2))
    field = int((es[0, 0] + mxfp7.SBIAS) << 3 | (m8[0, 0] - 8))
    assert field >> 3 == 0
    assert got[:4] == bytes([field] * 4)
