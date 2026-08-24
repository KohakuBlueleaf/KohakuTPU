"""Drive the memory mover on SILICON with whole-flit writes.

    python scripts/py/mover_silicon.py

`mover.issue` writes one 64-bit register at a time. The control window is
reached through a path that delivers a whole 32-byte flit per write, so a
single-register write lands its neighbours as ZERO -- and CTRL(0x00),
HDR(0x10) and DIM(0x18) share one flit, so writing DIM erases the HDR just set.

This composes each flit whole and writes it as one block. No RTL change.
"""

import time

import kohakutpu.units  # noqa: F401
from kohakuaccel.device import mover as MV
from kohakuaccel.transport.jtag import JtagTransport
from kohakutpu.host import Card

BOARD = "multimesh_v7"
FLIT = 32


def flit_bytes(regs: dict, base: int) -> bytes:
    """One 32-byte flit: four 64-bit slots at `base` +0 +8 +16 +24."""
    out = bytearray(FLIT)
    for off, val in regs.items():
        assert base <= off < base + FLIT, f"{off:#x} outside flit {base:#x}"
        at = off - base
        out[at : at + 8] = (val & ((1 << 64) - 1)).to_bytes(8, "little")
    return bytes(out)


def main() -> int:
    t = JtagTransport()
    card = Card.from_board(BOARD, transport=t, which=[0])
    m = card.mesh
    print(m)

    SRC, DST, W = 0x100000, 0x200000, 64
    src = bytes((i * 7 + 13) & 0xFF for i in range(W * 32))
    m.mem.write_block(SRC, src)
    m.mem.write_block(DST, b"\xa5" * (W * 32))
    assert m.mem.read_block(SRC, 32) == src[:32], "source paint failed"
    assert set(m.mem.read_block(DST, 32)) == {0xA5}, "dest paint failed"
    print(f"painted src {SRC:#x}, dst {DST:#x}, {W} words")

    sw = MV.Walker(base=SRC, dims=[(W, 32)])
    dw = MV.Walker(base=DST, dims=[(W, 32)])
    s_hdr, s_dim = sw.program(0)[0][1], sw.program(0)[1][1]
    d_hdr, d_dim = dw.program(1)[0][1], dw.program(1)[1][1]
    ctrl = MV.COPY | (MV.W16 << 3) | (MV.FLAG_WCOAL << 8) | (1 << 16)

    def put(regs, base):
        m.ctrl.write_block(MV.AUX_CFG + base, flit_bytes(regs, base))

    print("loading descriptors as whole flits")
    put({0x10: s_hdr, 0x18: s_dim}, 0x00)  # src header + dim, CTRL slot 0 (go=0)
    put({0x20: 0}, 0x20)  # latch pulse
    put({0x10: d_hdr, 0x18: d_dim}, 0x00)  # dst header + dim
    put({0x20: 0}, 0x20)  # latch pulse
    before = MV.status(m.ctrl.read64(MV.AUX_STAT))
    print(f"  before GO: {before}")

    # GO. HDR/DIM restated with the dst values so the re-pulse in this flit is
    # harmless -- it reloads what is already there instead of zeroing it.
    t0 = time.perf_counter()
    put({0x00: ctrl, 0x10: d_hdr, 0x18: d_dim}, 0x00)
    for _ in range(2000):
        st = MV.status(m.ctrl.read64(MV.AUX_STAT))
        if not st["busy"]:
            break
    el = time.perf_counter() - t0
    print(f"  after  GO: {st}   ({el*1000:.1f} ms wall)")

    got = m.mem.read_block(DST, W * 32)
    if got == src:
        print(f"\n*** COPY CORRECT ON SILICON: {W} words / {W*32} bytes ***")
    else:
        same = sum(1 for a, b in zip(got, src) if a == b)
        print(f"\nwrong: {same}/{len(src)} bytes match; first32 {got[:32].hex()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
