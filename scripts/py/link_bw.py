"""Measure an interlink: a buffer from one mesh's DRAM into another's.

    python scripts/py/link_bw.py                 # mesh_1 -> mesh_3 over link1
    python scripts/py/link_bw.py --src 0 --dst 2 # link1 the other side
    python scripts/py/link_bw.py --seconds 8     # run longer, tighter number

Measured 2026-08-13, mesh_1 -> mesh_3: 131,514,368 B in 1.34 s = 98 MB/s,
against 61 MB/s for the same mover writing its own mesh -- remote is faster
because a remote write is posted while a local one waits for a real DDR4 BRESP.
One packet and one beat per 32-byte word, so at the measured 100.09 MHz this
used 3.1% of the link's 3.20 GB/s: the MOVER is the bottleneck, not the wire.

A JTAG poll costs ~13 ms, so a move must run for SECONDS or the wall time is
just the poll -- hence a small source window re-read at outer stride 0 while
the destination walks distinct addresses.
"""

import argparse
import time

from kohakuaccel.device.registers import MAG_BASE
from kohakutpu.host import Card
from ktpu.hw import interlink as IL
from ktpu.hw.mover import WORD, Mover

#: Well clear of `kohakutpu.rt.ARENA_BASE` and of the driver's scratch.
SRC = 0x2000_0000
DST = 0x2800_0000

#: Source window, in words. 16 KB uploads in ~0.5 s over JTAG.
WINDOW = 512


class Board:
    """What `ktpu.hw.interlink` wants of a board: a name and `ctrl(offset)`.

    A :class:`kohakutpu.host.Mesh` is already rebased onto its own agent, so the
    offset it wants is the driver's logical one.
    """

    def __init__(self, mesh) -> None:
        self.name = f"mesh_{mesh.index}"

    @staticmethod
    def ctrl(off: int) -> int:
        return MAG_BASE + off


def pattern(words: int, tag: int) -> bytes:
    """Bytes whose every 32-byte word differs, so a shifted write is visible."""
    return b"".join(
        ((i * 0x0101_0101_0101_0101 + tag) & ((1 << 64) - 1)).to_bytes(8, "little") * 4
        for i in range(words)
    )


def push(mover, dst: int, reps: int, label: str) -> tuple:
    """`reps` passes of the source window into `dst`. Returns (bytes, lo, hi)."""
    mover.copy(
        SRC, dst, [(reps, 0), (WINDOW, WORD)], [(reps, WINDOW * WORD), (WINDOW, WORD)]
    )
    rec = mover.wait(timeout=600.0)
    nbytes = reps * WINDOW * WORD
    lo, hi = rec["last_busy"], rec["first_idle"]
    print(
        f"  {label}: {nbytes:,} B in [{lo:.3f}, {hi:.3f}] s ({rec['polls']} polls)"
        f" -> {nbytes / hi / 1e6:.1f}..{nbytes / lo / 1e6 if lo else 0:.1f} MB/s"
    )
    return nbytes, lo, hi


def mesh_clock(mesh, seconds: float = 3.0) -> float:
    """MHz, from mag_link's free-running idle counter. Trusts no constant."""

    def read():
        t = time.perf_counter()
        w = mesh.ctrl.read64(MAG_BASE + IL.IL_STALL1)
        return (w >> 32) & 0xFFFF_FFFF, (time.perf_counter() + t) / 2

    a, ta = read()
    time.sleep(seconds)
    b, tb = read()
    return ((b - a) % (1 << 32)) / (tb - ta) / 1e6


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=int, default=1, help="sending mesh")
    ap.add_argument("--dst", type=int, default=3, help="receiving mesh")
    ap.add_argument("--seconds", type=float, default=4.0, help="target move length")
    args = ap.parse_args()

    card = Card(which=[args.src, args.dst])
    dst = card.select(args.dst)
    src = card.select(args.src)
    print(card)

    sb, db = Board(src), Board(dst)
    caps = IL.caps(src.ctrl, sb)
    if caps is None:
        print("this bitstream has no interlink (IL_CAPS reads 0); nothing to measure")
        return 1
    print(f"{caps}, route {args.src}->{args.dst} = {IL.route(args.src, args.dst)}")
    print(f"mesh clock {mesh_clock(src):.2f} MHz")

    mover = Mover(src.ctrl, MAG_BASE)
    src.mem.write_block(SRC, pattern(WINDOW, 0xA5))
    # Faults are sticky, so an earlier session's would be reported as this run's.
    for mesh, board in ((src, sb), (dst, db)):
        IL.clear(mesh.ctrl, board)

    # 256 reps, not 16: a probe shorter than one 13 ms poll reads as far slower
    # than the mover is, and then picks far too few reps for the real run.
    print("\ncalibrating the mover's rate")
    probe = 256
    _, _, hi = push(mover, IL.global_addr(args.dst, DST, 4), probe, "probe")
    rate = probe * WINDOW * WORD / hi
    reps = max(16, min(0xFFFF, int(args.seconds * rate / (WINDOW * WORD))))
    print(f"  ~{rate / 1e6:.1f} MB/s -> {reps} reps for ~{args.seconds:.0f} s")

    # Deltas, not totals: IL_CTRL[1] clears the doorbells and NOT these.
    before = IL.counters(src.ctrl, sb)
    print(f"\nLOCAL  mesh_{args.src} -> mesh_{args.src}")
    lb, llo, lhi = push(mover, IL.global_addr(args.src, DST, 4), reps, "local")
    print(f"\nREMOTE mesh_{args.src} -> mesh_{args.dst}")
    rb, rlo, rhi = push(mover, IL.global_addr(args.dst, DST, 4), reps, "remote")
    after = IL.counters(src.ctrl, sb)

    print("\nverifying arrival")
    want = pattern(WINDOW, 0xA5)
    ok = all(
        dst.mem.read_block(DST + (r * WINDOW + w) * WORD, WORD)
        == want[w * WORD : (w + 1) * WORD]
        for r in (0, reps // 2, reps - 1)
        for w in (0, WINDOW - 1)
    )
    print(f"  sampled words match: {ok}")
    print(
        f"  faults: mesh_{args.src}={IL.faults(src.ctrl, sb)} "
        f"mesh_{args.dst}={IL.faults(dst.ctrl, db)}"
    )
    for link in ("link0", "link1"):
        d = {k: (after[link][k] - before[link][k]) % (1 << 32) for k in before[link]}
        print(f"  {link} tx delta: packets {d['tx_packets']:,} beats {d['tx_beats']:,}")

    print(
        f"\nBYTES {rb:,}  WALL [{rlo:.3f}, {rhi:.3f}] s"
        f"  -> {rb / rhi / 1e9:.3f}..{rb / rlo / 1e9:.3f} GB/s REMOTE"
    )
    print(
        f"BYTES {lb:,}  WALL [{llo:.3f}, {lhi:.3f}] s"
        f"  -> {lb / lhi / 1e9:.3f}..{lb / llo / 1e9:.3f} GB/s local"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
