"""Composed reads on the card model (L2.READ): each against the materialized
read of the same logical operand, or numpy.

    KOHAKUTPU_BOARD=multimesh_v9t6 KOHAKUTPU_SIM_BUILD=build/v9t6/vlt_card_v9t6_1n pytest ...
"""

import numpy as np
import pytest

from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import kernel as K

GUARD = 211
BROADCAST = "((16,0,0,256,((8,1),(32,0)),((8,32),(32,1))), (256,0,0,512,((8,32),(2,0),(32,1)),((8,64),(2,32),(32,1))))"


@pytest.fixture
def machine(dev):
    held = dev.machine
    dev.machine = machine_of(held.board, nodes=1, packing_backend="vector_mx7")
    yield dev.machine
    dev.machine = held


def values(seed, n):
    return np.random.default_rng(seed).standard_normal(n).astype(np.float16)


def run(dev, c, inputs, out, size):
    """`out`'s `size` bytes after `c` on `inputs`, its guards and inputs intact."""
    binds, held = {}, []
    params = {**inputs, out: bytes(size)}
    params.update({K.arena_name(a): bytes(a["bytes"]) for a in c.scratch})
    for name, raw in params.items():
        at = dev.alloc(len(raw) + 256)
        dev.write(at, bytes([GUARD]) * 128 + raw + bytes([GUARD]) * 128)
        binds[name] = at + 128
        held.append((at, name, raw))
    try:
        dev.run(c, binds)
        for at, name, raw in held:
            whole = dev.read(at, len(raw) + 256)
            assert whole[:128] == whole[-128:] == bytes([GUARD]) * 128, name
            assert name not in inputs or whole[128:-128] == raw, name
        return dev.read(binds[out], size)
    finally:
        for at, _, _ in held:
            dev.free(at)


def test_a_broadcast_chain_packs_as_its_materialized_operand(dev, machine):
    x = values(1, 16)
    composed = f"""target ktpu.v9
fn mapped.l2(x: f16[1,16] @dram, q: mx7a[8,64] @dram(gm=2,nk=1))
    quant q <- x read_map={BROADCAST} read_shape=(64,0,0,8,64) overlap=1
"""
    plain = """target ktpu.v9
fn plain.l2(y: f16[8,64] @dram, q: mx7a[8,64] @dram(gm=2,nk=1))
    quant q <- y
"""
    size = 8 * 64 * 2
    logical = np.repeat(x[:8, None], 64, axis=1)
    got = run(
        dev,
        K.compile(composed, name="mapped", machine=machine),
        {"x": x.tobytes()},
        "q",
        size,
    )
    want = run(
        dev,
        K.compile(plain, name="plain", machine=machine),
        {"y": logical.tobytes()},
        "q",
        size,
    )
    assert got == want


def test_an_irregular_tile_packs_as_its_materialized_operand(dev, machine):
    x = values(2, 4 * 72)
    composed = """target ktpu.v9
fn irregular.l2(x: f16[4,72] @dram, q: mx7a[4,32] @dram(gm=1,nk=1))
    quant q <- x read_map=((288,0,0,288,((4,72),(72,1)),((4,72),(72,1))),) read_shape=(72,0,0,4,32)
"""
    plain = """target ktpu.v9
fn plain.l2(y: f16[4,32] @dram, q: mx7a[4,32] @dram(gm=1,nk=1))
    quant q <- y
"""
    size = 128
    logical = x.reshape(4, 72)[:, :32]
    got = run(
        dev,
        K.compile(composed, name="irregular", machine=machine),
        {"x": x.tobytes()},
        "q",
        size,
    )
    want = run(
        dev,
        K.compile(plain, name="plain", machine=machine),
        {"y": logical.tobytes()},
        "q",
        size,
    )
    assert got == want


def test_a_mapped_load_reads_its_rows(dev, machine):
    x = values(3, 256)
    text = """target ktpu.v9
fn mapped_dsl.l2(x: f16[16,16] @dram, y: f16[16,128] @dram)
    par c in 0..2 on vc[c]
        for t in 0..4
            a = load x[:, :] read_map=((256,0,0,2048,((16,16),(8,0),(16,1)),((16,128),(8,16),(16,1))),) read_at=(c*8+t*2)*128 : f16[2,128] @l1
            v = unpack a : f32[2,128] @reg
            w = add v, 1.0 : f32[2,128] @reg
            b = pack w : f16[2,128] @l1
            store y[c*8+t*2:+2, :] <- b
"""
    c = K.compile(text, name="mapped_dsl", machine=machine)
    assert not c.verdict["refused"], c.verdict
    got = run(dev, c, {"x": x.tobytes()}, "y", 16 * 128 * 2)
    rows = np.tile(x.reshape(16, 16), (1, 8))
    want = (rows.astype(np.float32) + 1.0).astype(np.float16)
    assert got == want.tobytes()


def test_a_far_read_continues_past_a_descriptor_step(dev, machine):
    x = values(4, 2 * 65536)
    text = """target ktpu.v9
fn far.l2(x: f16[2,65536] @dram, y: f16[2,128] @dram)
    par c in 0..1 on vc[c]
        for t in 0..1
            a = load x[:, :] read_map=((131072,0,0,131072,((2,65536),(65536,1)),((2,65536),(65536,1))),) read_pitch=65536 : f16[2,128] @l1
            v = unpack a : f32[2,128] @reg
            b = pack v : f16[2,128] @l1
            store y[0:+2, :] <- b
"""
    c = K.compile(text, name="far", machine=machine)
    assert "__part1" in c.l1_text
    assert not c.verdict["refused"], c.verdict
    got = run(dev, c, {"x": x.tobytes()}, "y", 2 * 128 * 2)
    assert got == x.reshape(2, 65536)[:, :128].tobytes()
