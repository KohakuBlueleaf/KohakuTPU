"""MOVER steps: the compiler's mover encoding against the driver's, and relocation.

The compiler (`kohakuaccel.compiler.package.mover`) and the driver
(`kohakuaccel.driver.device.mover`) each write the mover's command encoding, and
neither may import the other; this is the test that ties the two copies.
"""

import pytest
from kohakuaccel.compiler.package import mover as PM
from kohakuaccel.compiler.package.build import PackageBuilder
from kohakuaccel.driver.device import mover as DM


def test_copy_encoding_matches_the_drivers():
    walk = [(4, 1 << 14), (16, 32)]
    want = DM.copy(
        DM.Walker(0x10_0000, dims=walk), DM.Walker(0x20_0000, dims=walk), ewidth=DM.W32
    )
    got = PM.move(PM.COPY, (0x10_0000, walk), (0x20_0000, walk), ewidth=PM.W32)
    assert got == want


def _dm(walk):
    """A compiler walker as the driver's: dims, axes, windows split out."""
    w = PM.Walk.of(walk)
    return DM.Walker(
        w.base,
        dims=[d[:2] for d in w.dims],
        axes=[d[2:] if len(d) == 4 else (0, 0) for d in w.dims],
        window={1: w.windows[0], 2: w.windows[1]},
    )


SRC = PM.Walk(0x10_0000, ((3, 1 << 12, 1, 1), (8, 32, 2, -1)), ((-1, 2), (0, 7)))
DST = PM.Walk(0x20_0000, ((24, 32),))


@pytest.mark.parametrize(
    "mine, theirs",
    [
        (
            PM.move(
                PM.COPY,
                SRC,
                DST,
                ewidth=PM.W16,
                imm=0x3C00,
                flags=PM.flag_byte(False, 3),
            ),
            DM.copy(_dm(SRC), _dm(DST), ewidth=DM.W16, imm=0x3C00, flags=3 << 5),
        ),
        (
            PM.fill(DST, 7, PM.W8),
            DM.fill(_dm(DST), 7, ewidth=DM.W8, flags=DM.FLAG_WCOAL),
        ),
        (
            PM.generate(DST, 0x1234),
            DM.program(DM.GENERATE, None, _dm(DST), ewidth=DM.W32, seed=0x1234),
        ),
        (
            PM.gather(0x30_0000, (0x40_0000, 9), DST, (256, 3), fp=True),
            DM.program(
                DM.GATHER,
                DM.Walker(0x30_0000, dims=[(1, 32)]),
                _dm(DST),
                ewidth=DM.W32,
                index=(0x40_0000, 9),
                gather=(256, 3),
                fp=True,
            ),
        ),
        (
            PM.scatter(DST, 0x30_0000, (0x40_0000, 9), (256, 3)),
            DM.program(
                DM.SCATTER,
                _dm(DST),
                DM.Walker(0x30_0000, dims=[(1, 32)]),
                ewidth=DM.W32,
                index=(0x40_0000, 9),
                gather=(256, 3),
            ),
        ),
    ],
    ids=["copy-axes-windows-pad-burst", "fill8", "generate", "gather-fp", "scatter"],
)
def test_every_mode_matches_the_drivers(mine, theirs):
    assert mine == theirs


def test_index_fields_sit_where_mm_mover_reads_them():
    """mm_mover.v's register case: 0x00 [2:0] mode, [17] fp32 index; 0x30
    [39:0] base, [55:40] count; 0x50 [31:0] pitch, [47:32] words."""
    w = dict(PM.scatter(DST, 0x30_0000, (0x40_0000, 9), (256, 3), fp=True))
    assert w[0x00] & 7 == 6 and w[0x00] >> 17 & 1 and w[0x00] >> 16 & 1
    assert w[0x30] & ((1 << 40) - 1) == 0x40_0000 and w[0x30] >> 40 == 9
    assert w[0x50] & 0xFFFF_FFFF == 256 and w[0x50] >> 32 == 3


def test_transform_fields_sit_where_the_slot_spec_puts_them():
    hdr = PM.walker(0, (0x40_0000, [(8, 32)]), xform_id=1, xform_mode=1)[0][1]
    assert (hdr >> 47) & 0xF == 1 and (hdr >> 55) & 0xF == 1
    assert (hdr >> 4) & ((1 << 40) - 1) == 0x40_0000 and (hdr >> 44) & 7 == 1
    ctrl = PM.convert(0x40_0000, 0x50_0000, 3)[-1][1]
    assert ctrl & 7 == 5 and ctrl >> 16 & 1


def test_a_long_conversion_is_several_whole_entry_moves():
    per = PM.MOVE_WORDS // 8  # entries a move carries
    entries = 3 * per + 5
    writes = PM.convert(0x40_0000, 0x400_0000, entries)
    gos = [v for r, v in writes if r == PM.R_CTRL]
    assert len(gos) == 4 and all(v & PM.GO for v in gos)
    hdrs = [v for r, v in writes if r == PM.R_HDR]
    src = [(v >> PM.BASE_LSB) & ((1 << 40) - 1) for v in hdrs[0::2]]
    dst = [(v >> PM.BASE_LSB) & ((1 << 40) - 1) for v in hdrs[1::2]]
    assert src == [0x40_0000 + k * per * 8 * 32 for k in range(4)]
    assert dst == [0x400_0000 + k * per * 4 * 32 for k in range(4)]
    counts = [(v >> 4) & 0xFFFF for r, v in writes if r == PM.R_DIM and not v & 1]
    assert counts == [per * 8] * 3 + [5 * 8]


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
