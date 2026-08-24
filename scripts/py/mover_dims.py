"""Which dimension actually latches? A walk whose ADDRESSES identify the dim.

    python scripts/py/mover_dims.py

Every mover register is write-only, so the descriptor cannot be read back. This
infers it from where the bytes land: a stride large enough that dim 0 and dim 1
write disjoint places, so the destination image names which dim ran.
"""

import kohakutpu.units  # noqa: F401
from kohakuaccel.device import mover as MV
from kohakuaccel.transport.jtag import JtagTransport
from kohakutpu.host import Card

BOARD = "multimesh_v7"
W, SRC, DST = 32, 0x100000, 0x2000000


def put(m, regs: dict, base: int, span: int = 32) -> None:
    buf = bytearray(span)
    for off, val in regs.items():
        buf[off - base : off - base + 8] = (val & ((1 << 64) - 1)).to_bytes(8, "little")
    m.ctrl.write_block(MV.AUX_CFG + base, bytes(buf))


def load(m, sel, walker) -> None:
    """ONE 32-byte flit per write. A 64-byte burst spans two flits and the
    dimension latch does not take -- measured: a 1-dim count of 4 moved one
    element."""
    prog = walker.program(sel)
    hdr = prog[0][1]
    for i in range(1, len(prog), 2):
        put(m, {0x10: hdr, 0x18: prog[i][1]}, 0x00)
        put(m, {0x20: prog[i + 1][1]}, 0x20)


def go(m, hdr, dim0) -> dict:
    """CTRL shares flit 0 with HDR(0x10) and DIM(0x18), so a bare GO write
    lands ndim=0 and base=0 on whichever walker `hdr`'s sel names -- every
    dimension dies and exactly ONE element moves. Restate them."""
    ctrl = MV.COPY | (MV.W16 << 3) | (MV.FLAG_WCOAL << 8) | (1 << 16)
    put(m, {0x00: ctrl, 0x10: hdr, 0x18: dim0}, 0x00)
    for _ in range(4000):
        st = MV.status(m.ctrl.read64(MV.AUX_STAT))
        if not st["busy"]:
            return st
    return st


def trial(m, name, src_dims, dst_dims, probe_words):
    """Run one descriptor; report which destination words got written."""
    m.mem.write_block(DST, b"\x00" * (max(probe_words) + 1) * W)
    for w in range(8):
        m.mem.write_block(SRC + w * W, bytes([0x10 + w]) * W)
    load(m, 0, MV.Walker(base=SRC, dims=src_dims))
    dw = MV.Walker(base=DST, dims=dst_dims)
    load(m, 1, dw)
    dp = dw.program(1)
    st = go(m, dp[0][1], dp[1][1])
    hit = []
    for w in probe_words:
        b = m.mem.read_block(DST + w * W, 8)[0]
        if b:
            hit.append((w, hex(b)))
    print(f"  {name:<34} fault={st['fault']:<6} written={hit}")


def main() -> int:
    t = JtagTransport()
    card = Card.from_board(BOARD, transport=t, which=[0])
    m = card.mesh
    print(
        f"{m}\nsrc words 0..7 hold 0x10..0x17; dst zeroed. "
        f"'written' names which dst words got a byte.\n"
    )

    # dim 0 alone: 4 words at stride 4*W -> dst words 0,4,8,12
    trial(m, "1-dim [(4, 4W)]", [(4, 4 * W)], [(4, 4 * W)], [0, 1, 4, 8, 12])
    # dim 1 alone, same shape
    trial(m, "1-dim [(4, W)]", [(4, W)], [(4, W)], [0, 1, 2, 3, 4])
    # 2-dim: outer 2 at stride 4W, inner 2 at stride W -> words 0,1,4,5
    trial(
        m,
        "2-dim [(2,4W),(2,W)]",
        [(2, 4 * W), (2, W)],
        [(2, 4 * W), (2, W)],
        [0, 1, 2, 3, 4, 5, 6, 7],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
