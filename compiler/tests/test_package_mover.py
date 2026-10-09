"""MOVER steps: the compiler's mover encoding, relocation, and the converting move.

The encoding is compared with the driver's (`kohakuaccel.device.mover`), the
independently written copy; the converting move's model against the MXFP7
packer, which scripts/py/sw_chain.py shows the RTL bank matches byte for byte.
"""

import numpy as np
from kohakuaccel.device import mover as DM
from kohakuaccel.package import mover as PM
from kohakuaccel.package.build import PackageBuilder
from kohakuaccel.package.interp import LocalNode
from kohakuaccel.package.lower import machine_units
from kohakuaccel.sim.machine import MEM_BASE, Memory
from kohakuaccel.sim.mailbox import SimMailbox
from kohakutpu.isa.fields import FIELDS
from kohakutpu.model import SimDevice, run_move

from kohakutpu import layout as LO
from kohakutpu import ops


def test_copy_encoding_matches_the_drivers():
    walk = [(4, 1 << 14), (16, 32)]
    want = DM.copy(
        DM.Walker(0x10_0000, dims=walk), DM.Walker(0x20_0000, dims=walk), ewidth=DM.W32
    )
    got = PM.move(PM.COPY, (0x10_0000, walk), (0x20_0000, walk), ewidth=PM.W32)
    assert got == want


def test_transform_fields_sit_where_the_slot_spec_puts_them():
    hdr = PM.walker(0, 0x40_0000, [(8, 32)], xform_id=1, xform_mode=1)[0][1]
    assert (hdr >> 47) & 0xF == 1 and (hdr >> 55) & 0xF == 1
    assert (hdr >> 4) & ((1 << 40) - 1) == 0x40_0000 and (hdr >> 44) & 7 == 1
    ctrl = PM.convert(0x40_0000, 0x50_0000, 3)[-1][1]
    assert ctrl & 7 == 5 and ctrl >> 16 & 1


def test_a_conversion_past_16_bits_is_several_whole_entry_moves():
    entries = 3 * 8191 + 5
    writes = PM.convert(0x40_0000, 0x400_0000, entries)
    gos = [v for r, v in writes if r == PM.R_CTRL]
    assert len(gos) == 4 and all(v & PM.GO for v in gos)
    hdrs = [v for r, v in writes if r == PM.R_HDR]
    src = [(v >> PM.BASE_LSB) & ((1 << 40) - 1) for v in hdrs[0::2]]
    dst = [(v >> PM.BASE_LSB) & ((1 << 40) - 1) for v in hdrs[1::2]]
    assert src == [0x40_0000 + k * 8191 * 8 * 32 for k in range(4)]
    assert dst == [0x400_0000 + k * 8191 * 4 * 32 for k in range(4)]
    counts = [(v >> 4) & 0xFFFF for r, v in writes if r == PM.R_DIM and not v & 1]
    assert counts == [8191 * 8] * 3 + [5 * 8]


def test_mover_bases_are_relocated():
    b = PackageBuilder()
    b.spans_from({0x40_0000: 0x1000, 0x50_0000: 0x1000})
    b.mover(PM.convert(0x40_0100, 0x50_0080, 2), addresses=PM.addresses)
    pkg = b.build(defaults=False)
    assert len(pkg.buffers) == 2 and len(pkg.relocs) == 2
    moved = pkg.bound([0x80_0000, 0x90_0000])
    regs = []
    for w in moved:
        for half in (0, 128):
            regs.append(((w >> half) & (2**64 - 1), (w >> (half + 64)) & (2**64 - 1)))
    bases = [(v >> 4) & ((1 << 40) - 1) for r, v in regs if r == PM.R_HDR]
    assert bases == [0x80_0100, 0x90_0080]


def test_converting_move_model_equals_the_packer():
    rng = np.random.default_rng(5)
    x = (rng.standard_normal((32, 64)) * 3).astype(np.float16)
    for side in (0, 1):
        mem = Memory()
        fp, mx = LO.Entry(8, 2), LO.MxEntry(8, 2, side)
        mem.write(0x1000, fp.pack(x))
        n = fp.nbytes(x.shape) // 256
        run_move(PM.convert(MEM_BASE + 0x1000, MEM_BASE + 0x8000, n, 1, side), mem)
        assert mem.read(0x8000, mx.nbytes(x.shape)) == mx.pack(x)


def _models(node: bool) -> SimDevice:
    dev = SimDevice(mg=((1, 1),), vc=((1, 0),), agent=(0, 1))
    if node:
        dev.node = LocalNode(
            SimMailbox(dev.card),
            machine_units(dev.machine),
            mover=lambda wr: run_move(wr, dev.card.mem),
        )
        dev.fields = FIELDS
        dev.keep_packages = True
    return dev


def test_a_produced_operand_is_converted_on_the_card():
    """vector -> matmul: y has no host copy, so it is walked and converted; w is
    uploaded FP16 and converted the same way."""
    rng = np.random.default_rng(3)
    a, b, w = (
        (rng.standard_normal((32, 64)) * 0.5).astype(np.float16) for _ in range(3)
    )
    got = {}
    for node in (False, True):
        dev = _models(node)
        got[node] = ops.matmul(
            ops.residual(dev.tensor(a), dev.tensor(b)), dev.tensor(w)
        ).numpy()
        assert dev.counters.get("quantised") == 2
    assert np.array_equal(got[False], got[True])
    plain = SimDevice(mg=((1, 1),), vc=((1, 0),), agent=(0, 1))
    want = ops.matmul(
        ops.residual(plain.tensor(a), plain.tensor(b)), plain.tensor(w)
    ).numpy()
    assert np.array_equal(got[True], want)
