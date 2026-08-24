"""Memory mover bandwidth ON SILICON, and what dispatch costs.

    python scripts/py/mover_bw_silicon.py

A size sweep separates the two terms: the fixed cost of pushing a descriptor
through the AXI-Lite control window, and the per-byte cost of the move itself.
If the fixed term dominates at realistic sizes, dispatch is the bottleneck and
no amount of mover bandwidth helps.

Descriptors go as BURSTS. A separate write64 per register arrives as its own
256-bit flit with the other 24 bytes zeroed, so it erases its neighbours --
CTRL(0x00), HDR(0x10) and DIM(0x18) share one flit. `sb_nmu` PACKs a burst into
one fully strobed flit, which is why a block write lands whole.
"""

import time

import kohakutpu.units  # noqa: F401
from kohakuaccel.device import mover as MV
from kohakuaccel.transport.jtag import JtagTransport
from kohakutpu.host import Card

BOARD = "multimesh_v7"
FLIT = 32
SRC, DST = 0x100000, 0x800000


def flit_bytes(regs: dict, base: int) -> bytes:
    out = bytearray(FLIT)
    for off, val in regs.items():
        out[off - base : off - base + 8] = (val & ((1 << 64) - 1)).to_bytes(8, "little")
    return bytes(out)


def run_copy(m, words: int) -> tuple:
    """One COPY of `words` 32-byte words. Returns (dispatch_s, move_s)."""
    sw = MV.Walker(base=SRC, dims=[(words, 32)])
    dw = MV.Walker(base=DST, dims=[(words, 32)])
    s_hdr, s_dim = sw.program(0)[0][1], sw.program(0)[1][1]
    d_hdr, d_dim = dw.program(1)[0][1], dw.program(1)[1][1]
    ctrl = MV.COPY | (MV.W16 << 3) | (MV.FLAG_WCOAL << 8) | (1 << 16)

    def put(regs, base):
        m.ctrl.write_block(MV.AUX_CFG + base, flit_bytes(regs, base))

    t0 = time.perf_counter()
    put({0x10: s_hdr, 0x18: s_dim}, 0x00)
    put({0x20: 0}, 0x20)
    put({0x10: d_hdr, 0x18: d_dim}, 0x00)
    put({0x20: 0}, 0x20)
    t1 = time.perf_counter()
    put({0x00: ctrl, 0x10: d_hdr, 0x18: d_dim}, 0x00)
    for _ in range(20000):
        if not MV.status(m.ctrl.read64(MV.AUX_STAT))["busy"]:
            break
    t2 = time.perf_counter()
    return t1 - t0, t2 - t1


def main() -> int:
    t = JtagTransport()
    card = Card.from_board(BOARD, transport=t, which=[0])
    m = card.mesh
    print(f"{m}\nMEMORY MOVER, ON SILICON. Descriptors as bursts.\n")

    print(
        f"{'words':>8}{'bytes':>10} {'dispatch ms':>12} {'move ms':>9} "
        f"{'MB/s':>9}  correct"
    )
    rows = []
    for words in (32768, 262144, 1048576, 4194304):
        nbytes = words * 32
        src = bytes((i * 7 + 13) & 0xFF for i in range(min(nbytes, 4096)))
        m.mem.write_block(SRC, src * (nbytes // len(src)))
        m.mem.write_block(DST, b"\xa5" * min(nbytes, 65536))
        disp, move = run_copy(m, words)
        chk = m.mem.read_block(DST, 64) == (src * (nbytes // len(src)))[:64]
        mbs = nbytes / move / 1e6 if move > 0 else 0
        rows.append((nbytes, disp, move))
        print(
            f"{words:>8}{nbytes:>10,} {disp*1000:>12.1f} {move*1000:>9.1f} "
            f"{mbs:>9.1f}  {'yes' if chk else 'NO'}"
        )

    if len(rows) > 1:
        db = rows[-1][0] - rows[0][0]
        dm = rows[-1][2] - rows[0][2]
        print(
            f"\nslope: +{db:,} B / +{dm*1000:.1f} ms = "
            f"{db/dm/1e6:.1f} MB/s marginal"
        )
        print(f"fixed dispatch cost: ~{rows[0][1]*1000:.1f} ms a descriptor")
        print(
            f"  -> a move must exceed {rows[0][1]*db/dm/1e6*1000:,.0f} KB "
            f"before the move outweighs its own dispatch"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
