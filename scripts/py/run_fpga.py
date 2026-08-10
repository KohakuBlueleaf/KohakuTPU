"""Run one matmul on the CARD, batch style. The bring-up entry point.

    python run_fpga.py --probe                 # is anything there?
    python run_fpga.py --m 16 --k 32 --n 16    # run one shape

Shapes are M, K, N throughout this driver: `C[M,N] = A[M,K] @ B.T[N,K]`.

The simulator's sibling is `run_matmul.py`, and the two print the same figures
because both score through :func:`ktpu.hw.sim.payload`. A card has no cycle
counters, so the profile and the timeline are absent rather than estimated.

`--probe` FIRST on a machine that has not been run before: it reads one
constant register, which separates "the card is not there" from "the answer is
wrong", and every later failure hides that distinction.
"""

import argparse
import sys

from ktpu.hw import bench, fpga, fromdsl
from ktpu.hw.board import BOARDS, Board
from ktpu.hw.device import TransportUnavailable
from ktpu.hw.fpga import HardwareError, Session


def probe(s):
    """Print what the machine says about itself, and what the board expects."""
    b = s.board
    print(f"board      {b.name}  {b.layout}  {b.ncl} clusters  {b.ncol}x{b.nrow}")
    print(f"  ctrl     {b.ctrl_base:#013x}   mem {b.mem_base:#013x}")
    print(f"  clusters {[tuple(c) for c in b.clusters]}")
    print(f"  quantise {'reachable' if b.quant_bit is not None else 'UNREACHABLE'}")
    print(f"  orch     {b.orchestrator}")
    for line in b.verify():
        print(f"  MISMATCH vs bench.mesh: {line}")

    c = s.handshake()
    print(f"machine    A_CAPS {c['raw']:#018x}")
    print(
        f"  flit {c['flit_width']}  pos {c['pos_width']}  grid {c['grid_lo']}..{c['grid_hi']}"
    )
    print(f"counters   {s.counters()}")

    # Ask every node what it IS. The board file is a claim; this is the answer.
    print("\nnodes      CU_CTRL read of CU_CAPS, over the agent's mailbox")
    for name, caps in s.enumerate_nodes().items():
        if "error" in caps:
            print(f"  ({name:5s}) NO ANSWER -- {caps['error']}")
        else:
            print(
                f"  ({name:5s}) {caps['name']!r} 0x{caps['type']:04X} "
                f"v{caps['version']}  buffers {caps['buffers']}  "
                f"inst_depth {caps['inst_depth']}"
            )
    for line in s.check_nodes():
        print(f"  MISMATCH {line}")
    return 0


def ladder(lad):
    """The same matmul in every candidate format, with the card as one column.

    THE TABLE IS THE POINT. Correctness here is a position, not a number under
    a limit: a right circuit lands beside its own format and the whole ladder
    moves together when the operands are extreme. Read down the `dist` column
    -- the answer should be nearest its own model and nowhere near the rest.
    """
    own = lad["own"]
    print(
        f"\n  FORMAT LADDER   C[{lad['rows']},{lad['cols']}] of "
        f"[{lad['of'][0]},{lad['of'][1]}], K={lad['k']}"
    )
    print(
        f"    {'format':22s} {'rel p50':>10s} {'p99':>10s} "
        f"{'>10%':>7s} {'blown':>6s} {'dist to answer':>15s}"
    )
    for nm, s in lad["formats"].items():
        d = lad["distance"][nm]
        tag = "  <- the machine's own" if nm == own else ""
        tag = "  <- nearest" if nm == lad["nearest"] and not tag else tag
        print(
            f"    {nm:22s} {s['p50']:10.3e} {s['p99']:10.3e} {s['over10']:7d} "
            f"{s['blown']:6d} {d:15.3e}{tag}"
        )
    h = lad["hardware"]
    print(
        f"    {'THE ANSWER':22s} {h['p50']:10.3e} {h['p99']:10.3e} "
        f"{h['over10']:7d} {h['blown']:6d}"
    )
    if "cross_check" in lad:
        print(
            f"    two independent mxfp7 implementations differ by "
            f"{lad['cross_check']:.2e}"
        )


def report(r):
    """Print one run. A failure never reads as a passing detail.

    The checks are printed GROUPED -- what decided the verdict, then what only
    reports -- because scattering a 9.65e-01 among the oks as `note` is how a
    run with four failed checks came to look like a clean one.
    """
    v = r["verdict"]
    print(f"\n{r['shape']['m']}x{r['shape']['k']}x{r['shape']['n']}  tile {r['tile']}")
    print(f"  {r['passes']} passes in {r['rounds']} rounds on {r['ncl']} clusters")
    print(f"  {r['seconds']:.3f}s wall   {r['transport']}")
    print(f"  sig_done {r['counters']['sig_done']}")
    for name, node in r["counters"]["nodes"].items():
        print(f"    cluster {name}: {node['signals']} signals, valid={node['valid']}")

    ladder(r["ladder"])

    gating = [c for c in v["checks"] if c["gates"]]
    rest = [c for c in v["checks"] if not c["gates"]]
    print("\n  decides the verdict")
    for c in gating:
        print(f"    {'ok  ' if c['pass'] else 'FAIL'} {c['name']}: {c['text']}")
    if rest:
        print("  reported only")
        for c in rest:
            mark = "ok  " if c["pass"] else "MISS"
            print(f"    {mark} {c['name']}: {c['text']}")
    # Named again, together. A reader who skims the list should still not be
    # able to miss that something outside its reference was seen.
    if v["advisories"]:
        print(
            f"\n  ADVISORY: {len(v['advisories'])} check(s) outside reference "
            f"-- {', '.join(v['advisories'])}"
        )
    print("\nPASS" if v["pass"] else "\nFAIL")
    return 0 if v["pass"] else 1


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--m", type=int, default=16)
    ap.add_argument("--k", type=int, default=32)
    ap.add_argument("--n", type=int, default=16)
    ap.add_argument("--board", help="a name in boards/, a .json path, or $KTPU_BOARD")
    ap.add_argument("--transport", help="xdma[:n], jtag, memory (default: the board's)")
    ap.add_argument("--seed", type=lambda v: int(v, 0), default=0xC0FFEE)
    ap.add_argument("--dist", default="lowrank")
    ap.add_argument("--use", type=int, help="clusters to dispatch to (default: all)")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument(
        "--probe", action="store_true", help="identify the machine and stop"
    )
    ap.add_argument("--list-boards", action="store_true")
    ap.add_argument(
        "--dsl",
        action="store_true",
        help="run the DSL COMPILER's machine code instead of the planner's. "
        "Everything else -- operands, upload, control program -- is identical, "
        "so a difference in the answer is a difference in the compiler",
    )
    ap.add_argument(
        "--prefill",
        action="store_true",
        help="mark the image first; the DRAM has ECC, so a never-written line "
        "reads as a bus error rather than as zeros",
    )
    args = ap.parse_args()

    if args.list_boards:
        for path in sorted(BOARDS.glob("*.json")):
            b = Board.load(path)
            print(
                f"{path.stem:24s} {b.layout:7s} {b.ncl} clusters  "
                f"ctrl {b.ctrl_base:#x}"
            )
        return 0

    try:
        with Session(
            board=args.board, transport=args.transport, timeout=args.timeout
        ) as s:
            if args.probe:
                return probe(s)
            why = s.check(args.m, args.k, args.n, fpga.PREQ, args.use)
            if why:
                print("cannot run this shape here:", file=sys.stderr)
                for w in why:
                    print(f"  {w}", file=sys.stderr)
                return 2
            if args.prefill:
                prob = bench.build(
                    args.m,
                    args.k,
                    args.n,
                    args.seed,
                    ncl=s.board.ncl,
                    dist=args.dist,
                    preq=fpga.PREQ,
                    mesh_layout=s.board.layout,
                    caps=s.board.caps(),
                )
                print(f"prefilled {s.prefill(prob)} bytes")
            transform = None
            if args.dsl:
                # The DSL compiler lowered for THIS board -- its capacities, its
                # cluster count, its features -- not for the current RTL's.
                target = s.board.target(clusters=1)
                print(
                    f"dsl        lowering for {target.name}, "
                    f"tiles={target.tiles} l1={target.l1_a}/{target.l1_b}"
                )

                def transform(prob, t=target):
                    return fromdsl.restage(prob, args.m, args.k, args.n, t)

            return report(
                s.run(
                    args.m,
                    args.k,
                    args.n,
                    seed=args.seed,
                    dist=args.dist,
                    use=args.use,
                    transform=transform,
                )
            )
    except TransportUnavailable as exc:
        print(f"no machine to run on: {exc}", file=sys.stderr)
        return 3
    except HardwareError as exc:
        print(f"the machine did not answer as expected:\n{exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
