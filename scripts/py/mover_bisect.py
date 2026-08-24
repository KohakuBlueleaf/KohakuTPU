"""Bisect from the one PROVEN mover sequence to the failing one.

    python scripts/py/mover_bisect.py

`mover_silicon.py` moved 64 words byte-exact. `mover_dims.py`, which looks the
same, moves one. This starts from the proven call and changes ONE thing at a
time, checking the whole destination each run.
"""

import kohakutpu.units  # noqa: F401
from kohakuaccel.device import mover as MV
from kohakuaccel.transport.jtag import JtagTransport
from kohakutpu.host import Card

BOARD = "multimesh_v7"
FLIT = 32


def flit(regs: dict, base: int) -> bytes:
    out = bytearray(FLIT)
    for off, val in regs.items():
        out[off - base : off - base + 8] = (val & ((1 << 64) - 1)).to_bytes(8, "little")
    return bytes(out)


def copy(m, words, src, dst, src_dims=None, dst_dims=None) -> dict:
    """The sequence proven in mover_silicon.py, parameterised."""
    sw = MV.Walker(base=src, dims=src_dims or [(words, 32)])
    dw = MV.Walker(base=dst, dims=dst_dims or [(words, 32)])
    sp, dp = sw.program(0), dw.program(1)
    ctrl = MV.COPY | (MV.W16 << 3) | (MV.FLAG_WCOAL << 8) | (1 << 16)

    def put(regs, base):
        m.ctrl.write_block(MV.AUX_CFG + base, flit(regs, base))

    # 0x28 CARRIES sel. It is R_WINDOW: its bit 0 assigns `ld_sel`, and it
    # pulses `d_ax_en`, which is the ONLY writer of `d_aext` -- cleared
    # otherwise only by reset. A zero here aims every window write at the
    # SOURCE, so the destination keeps a stale extent, `dst_valid` goes low and
    # the writes are suppressed while the walk still completes with no fault.
    for sel, prog in ((0, sp), (1, dp)):
        hdr = prog[0][1]
        for i in range(1, len(prog), 2):
            put({0x10: hdr, 0x18: prog[i][1]}, 0x00)
            put({0x20: prog[i + 1][1], 0x28: sel}, 0x20)
    put({0x00: ctrl, 0x10: dp[0][1], 0x18: dp[1][1]}, 0x00)
    for _ in range(4000):
        st = MV.status(m.ctrl.read64(MV.AUX_STAT))
        if not st["busy"]:
            break
    return st


def check(m, dst, src_img, words) -> str:
    got = m.mem.read_block(dst, words * 32)
    if got == src_img[: words * 32]:
        return "ALL CORRECT"
    n = sum(
        1
        for i in range(words)
        if got[i * 32 : (i + 1) * 32] == src_img[i * 32 : (i + 1) * 32]
    )
    return f"{n}/{words} words"


def main() -> int:
    t = JtagTransport()
    card = Card.from_board(BOARD, transport=t, which=[0])
    m = card.mesh
    print(f"{m}\n")

    img = bytes((i * 7 + 13) & 0xFF for i in range(64 * 32))

    for label, words, src, dst in (
        ("proven:  64 words, src 0x100000 dst 0x200000", 64, 0x100000, 0x200000),
        ("count:    4 words, same addresses", 4, 0x100000, 0x200000),
        ("count:   16 words, same addresses", 16, 0x100000, 0x200000),
        ("addr:    64 words, dst 0x2000000", 64, 0x100000, 0x2000000),
    ):
        m.mem.write_block(src, img)
        m.mem.write_block(dst, b"\xa5" * (64 * 32))
        st = copy(m, words, src, dst)
        print(f"  {label:<46} fault={st['fault']:<5} {check(m, dst, img, words)}")

    print()
    for label, sd, dd in (
        (
            "2-dim, both [(2,32*4),(4,32)]  = 8 words",
            [(2, 128), (4, 32)],
            [(2, 128), (4, 32)],
        ),
        (
            "2-dim, both [(4,32*4),(4,32)]  = 16 words",
            [(4, 128), (4, 32)],
            [(4, 128), (4, 32)],
        ),
    ):
        m.mem.write_block(0x100000, img)
        m.mem.write_block(0x200000, b"\xa5" * (64 * 32))
        words = sd[0][0] * sd[1][0]
        st = copy(m, words, 0x100000, 0x200000, sd, dd)
        print(
            f"  {label:<46} fault={st['fault']:<5} " f"{check(m, 0x200000, img, words)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
