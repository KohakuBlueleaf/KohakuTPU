"""What the memory mover can actually do, ON SILICON. Copy, transpose, strided.

    python scripts/py/mover_capability.py

Every result is read back from DRAM and checked against numpy. A rate for a move
that produced wrong bytes is worthless, so nothing is timed that is not checked.

`count` is 16 bits, so a large move NESTS dimensions rather than using one huge
count -- the walker has six, and the destination defines the iteration space.
Descriptors go as BURSTS: a lone write64 arrives as its own 256-bit flit with
the other 24 bytes zeroed, and CTRL/HDR/DIM share one flit.
"""

import time

import kohakutpu.units  # noqa: F401
import numpy as np
from kohakuaccel.device import mover as MV
from kohakuaccel.transport.jtag import JtagTransport
from kohakutpu.host import Card

BOARD = "multimesh_v7"
FLIT, W = 32, 32  # flit bytes, and a mover word is 32 bytes
SRC, DST = 0x100000, 0x2000000


def flit(regs: dict, base: int) -> bytes:
    out = bytearray(FLIT)
    for off, val in regs.items():
        out[off - base : off - base + 8] = (val & ((1 << 64) - 1)).to_bytes(8, "little")
    return bytes(out)


def move(m, src_dims, dst_dims, src=SRC, dst=DST) -> float:
    """Load both walkers as whole flits, fire, wait. Returns wall seconds."""
    sw = MV.Walker(base=src, dims=src_dims)
    dw = MV.Walker(base=dst, dims=dst_dims)
    sp, dp = sw.program(0), dw.program(1)
    ctrl = MV.COPY | (MV.W16 << 3) | (MV.FLAG_WCOAL << 8) | (1 << 16)

    def put(regs, base):
        m.ctrl.write_block(MV.AUX_CFG + base, flit(regs, base))

    # (R_HDR, v) then per dim (R_DIM, v), (R_AXIS, v). HDR and DIM share flit 0;
    # the AXIS write at 0x20 is the latch pulse for the dim staged before it.
    # ONE 64-BYTE BURST PER DIMENSION. The bench writes 0x18 then 0x20 as
    # back-to-back single registers; here 0x10 and 0x18 share a 32-byte beat, so
    # stage and latch go in one transaction (0x00..0x3F) to keep that adjacency.
    def load(sel, prog):
        hdr = prog[0][1]
        for i in range(1, len(prog), 2):
            buf = bytearray(64)
            for off, val in (
                (0x10, hdr),
                (0x18, prog[i][1]),
                (0x20, prog[i + 1][1]),
                (0x28, sel),
            ):
                buf[off : off + 8] = (val & ((1 << 64) - 1)).to_bytes(8, "little")
            m.ctrl.write_block(MV.AUX_CFG, bytes(buf))

    load(0, sp)
    load(1, dp)
    t0 = time.perf_counter()
    put({0x00: ctrl, 0x10: dp[0][1], 0x18: dp[1][1]}, 0x00)
    for _ in range(40000):
        st = MV.status(m.ctrl.read64(MV.AUX_STAT))
        if not st["busy"]:
            break
    el = time.perf_counter() - t0
    if st["fault_code"]:
        print(f"    FAULT {st['fault_code']}: {st['fault']}")
    return el


def main() -> int:
    t = JtagTransport()
    card = Card.from_board(BOARD, transport=t, which=[0])
    m = card.mesh
    print(f"{m}\nMEMORY MOVER CAPABILITY, ON SILICON -- every move checked\n")

    # ---- 1. contiguous copy, dimensions NESTED so count stays 16-bit -------
    # A 4x4 word transpose: the smallest 2-dim case where the two walkers
    # disagree, so a failure here is the descriptor and not the size.
    print("0. SMALLEST DISAGREEING CASE: 4x4 word transpose")
    img4 = bytes((i * 13 + 5) & 0xFF for i in range(4 * 4 * W))
    m.mem.write_block(SRC, img4)
    m.mem.write_block(DST, b"\x00" * (4 * 4 * W))
    move(m, [(4, W), (4, 4 * W)], [(4, 4 * W), (4, W)])
    g4 = np.frombuffer(m.mem.read_block(DST, 4 * 4 * W), dtype=np.uint8)
    a4 = np.frombuffer(img4, dtype=np.uint8).reshape(4, 4, W)
    print(
        f"   transpose 4x4: "
        f"{'correct' if np.array_equal(g4.reshape(4, 4, W), a4.transpose(1, 0, 2)) else 'WRONG'}"
    )
    move(m, [(4, 4 * W), (4, W)], [(4, 4 * W), (4, W)])
    g4 = m.mem.read_block(DST, 4 * 4 * W)
    print(
        f"   plain copy 4x4 (same strides): "
        f"{'correct' if g4 == img4 else 'WRONG'}\n"
    )

    # CHECK THE END, NOT JUST THE START. Reading only the first 4 KB passes
    # even when the OUTER dimension never advanced -- the inner loop alone
    # fills it. The last word is what proves the whole walk ran.
    print("1. CONTIGUOUS COPY (nested dims; head AND tail checked)")
    print(f"{'words':>9}{'bytes':>12} {'wall ms':>9} {'MB/s':>9}  head  tail")
    pat = bytes((i * 7 + 13) & 0xFF for i in range(4096))
    for outer, inner in ((2, 64), (16, 512), (256, 512)):
        words = outer * inner
        nbytes = words * W
        reps = -(-nbytes // len(pat))
        m.mem.write_block(SRC, (pat * reps)[:nbytes])
        m.mem.write_block(DST, b"\xa5" * nbytes)
        el = move(m, [(outer, inner * W), (inner, W)], [(outer, inner * W), (inner, W)])
        head = m.mem.read_block(DST, 512) == (pat * reps)[:512]
        tail = (
            m.mem.read_block(
                DST,
                512,
            )
            if nbytes <= 512
            else m.mem.read_block(DST + nbytes - 512, 512)
            == (pat * reps)[nbytes - 512 : nbytes]
        )
        print(
            f"{words:>9,}{nbytes:>12,} {el*1000:>9.1f} "
            f"{nbytes/el/1e6:>9.1f}  {'ok ' if head else 'BAD'}  "
            f"{'ok ' if tail else 'BAD'}"
        )

    # ---- 2. word transpose: source strides swapped -------------------------
    print("\n2. WORD TRANSPOSE  dst[j][i] = src[i][j]")
    print(f"{'R x C':>11}{'bytes':>10} {'wall ms':>9} {'MB/s':>9}  correct")
    for r, c in ((32, 32), (64, 64), (128, 128)):
        nbytes = r * c * W
        img = np.arange(r * c * W // 2, dtype=np.uint16).tobytes()[:nbytes]
        m.mem.write_block(SRC, img)
        m.mem.write_block(DST, b"\x00" * min(nbytes, 1 << 20))
        # dst walks (j, i); src is walked in lockstep at swapped strides
        el = move(m, [(c, W), (r, c * W)], [(c, r * W), (r, W)])
        got = m.mem.read_block(DST, nbytes)
        a = np.frombuffer(img, dtype=np.uint8).reshape(r, c, W)
        ok = np.array_equal(
            np.frombuffer(got, dtype=np.uint8).reshape(c, r, W), a.transpose(1, 0, 2)
        )
        print(
            f"{r:>5} x{c:>4}{nbytes:>10,} {el*1000:>9.1f} "
            f"{nbytes/el/1e6:>9.1f}  {'yes' if ok else 'NO'}"
        )

    # ---- 3. strided gather: runs lifted out of rows ------------------------
    print("\n3. STRIDED  (runs of RUN words lifted from rows of ROW, packed)")
    ROW, RUN, RUNS = 256, 8, 256
    nbytes = RUNS * RUN * W
    img = bytes((i * 31 + 7) & 0xFF for i in range(ROW * RUNS * W))
    m.mem.write_block(SRC, img)
    m.mem.write_block(DST, b"\x00" * nbytes)
    el = move(m, [(RUNS, ROW * W), (RUN, W)], [(RUNS, RUN * W), (RUN, W)])
    got = m.mem.read_block(DST, nbytes)
    want = b"".join(img[i * ROW * W : i * ROW * W + RUN * W] for i in range(RUNS))
    print(
        f"  {RUNS} runs x {RUN} words: {nbytes:,} B in {el*1000:.1f} ms = "
        f"{nbytes/el/1e6:.1f} MB/s  {'correct' if got == want else 'WRONG'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
