"""What else will this card accept? One session, many configurations, one table.

    python sweep_card.py

Breadth rather than depth: every row is a full `Session.run`, scored by the
same `sim.payload` as everything else, so a row that says PASS says it for the
same reason `run_fpga.py` does. A configuration the board REFUSES is a result
too and gets its refusal printed verbatim -- the version gate and the capacity
check are supposed to say no, and a sweep that hid that would be reporting the
absence of a test as a pass.
"""

import argparse
import sys
import time

from ktpu.hw.device import TransportUnavailable
from ktpu.hw.fpga import HardwareError, Session

#: (m, k, n, use, packing). `use=None` is every cluster the board has.
CONFIGS = [
    (64, 1024, 64, None, "spread"),
    (64, 512, 64, None, "spread"),
    (64, 256, 64, None, "spread"),
    (64, 128, 64, None, "spread"),
    (64, 64, 64, None, "spread"),
    (256, 512, 256, None, "spread"),
    (512, 64, 512, None, "spread"),
]


def row(s, m, k, n, use, packing, timeout):
    """Run one configuration and return its line of the table."""
    name = f"{m}x{k}x{n}"
    tag = f"use={use or s.board.ncl} {packing}"
    why = s.check(m, k, n, use=use)
    if why:
        return f"  {name:14s} {tag:14s} REFUSED  {'; '.join(why)}"
    t0 = time.time()
    try:
        r = s.run(m, k, n, use=use, packing=packing)
    except (HardwareError, ValueError, OSError) as exc:
        return f"  {name:14s} {tag:14s} ERROR    {str(exc).splitlines()[0]}"
    v, lad = r["verdict"], r["ladder"]
    fails = [c["name"] for c in v["checks"] if c["gates"] and not c["pass"]]
    return (
        f"  {name:14s} {tag:14s} {'PASS' if v['pass'] else 'FAIL':6s} "
        f"{r['passes']:4d}p/{r['rounds']:2d}r  "
        f"p50 {lad['hardware']['p50']:.2e}  "
        f">10% {lad['hardware']['over10']:4d} vs model {lad['formats'][lad['own']]['over10']:4d}"
        f"  excess {lad['excess']:+3d}  detach {lad['detach']:.3f}"
        f"  model-to-model {lad.get('cross_check', float('nan')):.2e}"
        f"  {time.time() - t0:6.1f}s"
        + (f"  FAILED: {','.join(fails)}" if fails else "")
    )


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--board")
    ap.add_argument("--transport", default="jtag")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument(
        "--max-block",
        type=lambda v: int(v, 0),
        default=1 << 23,
        help="JTAG's refusal threshold, raised so the big shapes reach readback. "
        "The guard exists to stop a surprise multi-minute transfer, and a sweep "
        "that means to pay it should say so rather than trip over it",
    )
    args = ap.parse_args()

    try:
        transport = args.transport
        if transport == "jtag":
            from ktpu.hw.jtag import JtagTransport

            transport = JtagTransport(max_block=args.max_block)
        with Session(board=args.board, transport=transport, timeout=args.timeout) as s:
            s.handshake()
            print(
                f"board {s.board.name}  {s.board.ncl} clusters  "
                f"tiles={s.board.tiles} l1={s.board.l1_a}/{s.board.l1_b}"
            )
            for m, k, n, use, packing in CONFIGS:
                print(row(s, m, k, n, use, packing, args.timeout), flush=True)
            return 0
    except TransportUnavailable as exc:
        print(f"no machine to run on: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
