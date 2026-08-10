"""What the card ACHIEVES against what it could, measured rather than assumed.

    python perf_card.py --what vector      # exact, from the core's own counter
    python perf_card.py --what cluster     # wall clock, transport subtracted
    python perf_card.py --what all

THE TWO HALVES ARE NOT THE SAME KIND OF MEASUREMENT and are reported apart so
nobody averages them.

A VECTOR core counts its own cycles: `vec_cu.v` returns `cycles` as the RUN's
`exec_result` and the agent mirrors it into NODE_STATUS. Every vector figure
here is that counter, and each one is a SLOPE -- the same kernel at two trip
counts, differenced -- so fixed overhead cancels and nothing is estimated.

A CLUSTER counts nothing. `mx_cluster_cu.v`'s `exec_result` is
`nfill + ngemm + ndrain`, an instruction count, and the orchestrator has no
timer register, so there is no cycle count to read at any level. The cluster
figures are therefore WALL CLOCK with the transport's measured per-transaction
cost subtracted, which is a bound and not a measurement -- see the caveat the
report prints next to them.
"""

import argparse
import statistics
import sys
import time

from ktpu.hw import bench
from ktpu.hw import device as dev
from ktpu.hw import vector as V
from ktpu.hw.device import TransportUnavailable
from ktpu.hw.fpga import PREQ, HardwareError, Session

#: 300 MHz, and one cluster's 307.2 GFLOP/s -- 1024 FLOP per cycle.
CLOCK_HZ = 300e6
CLUSTER_PEAK = 307.2e9

#: `docs/isa/vector.md` §6: 16 ALUs, one result each per cycle.
VEC_ALUS = 16

SRC = 0x0010_0000
DST = 0x0011_0000


# --------------------------------------------------------------------------
# the transport, measured before anything is attributed to it
# --------------------------------------------------------------------------
def transaction_cost(s, n=40):
    """Seconds per JTAG register access, from reads of a constant register.

    A_CAPS never changes, so this times the round trip and nothing else. The
    median is reported because Vivado's Tcl server occasionally stalls a call
    by tens of milliseconds and a mean would carry that into every subtraction.
    """
    addr = dev.MAG_BASE + dev.A_CAPS
    times = []
    for _ in range(n):
        t0 = time.perf_counter()
        s.link.read64(addr)
        times.append(time.perf_counter() - t0)
    return statistics.median(times), min(times), max(times)


def block_rate(s, nbytes=1 << 15):
    """Bytes per second for a bulk write into device memory."""
    payload = bytes(nbytes)
    t0 = time.perf_counter()
    s.raw.write_block(s.board.mem_byte(0) + SRC, payload)
    return nbytes / (time.perf_counter() - t0)


# --------------------------------------------------------------------------
# the vector core: exact cycles
# --------------------------------------------------------------------------
def vector_cycles(s, node, kernel, timeout=30.0):
    """Run one kernel and return its cycle count, raising on a fault."""
    st = V.run_kernel(s.link, node, kernel.flits(), timeout)
    if st["fault"]:
        raise HardwareError(f"kernel faulted: {st['why']}")
    return st["arg"]


def _preamble(k, vl, mode=V.FLAT):
    """Set VL and the lane topology, then fill L1 and load v0/v1 with real data.

    Real data rather than whatever the register file powered up holding: a
    denormal or a NaN is not a timing hazard in this pipeline, but leaving it
    to chance makes the number unreproducible for no gain.
    """
    words = vl * 2 // V.WORD_BYTES
    k.desc(0, SRC, V.dim(V.WORD_BYTES, 2 * words))
    k.desc(1, 0, V.dim(1, words))
    k.desc(2, words, V.dim(1, words))
    k.seti(0, vl)
    k.emit(V.vsetvl(0), V.vsetmode(mode), V.vfill(0), V.vbar())
    k.emit(V.vld(0, 1), V.vld(1, 2))
    return k


def alu_loop(trips, vl=128, mode=V.FLAT, body=None):
    """A VLOOP of `trips` iterations over `body`, everything else fixed.

    Four destinations rather than one: `vec_core.v` gates issue on a pending
    write to the DESTINATION as well as the sources, so a body that rewrote one
    register would measure the 14-deep pipe's latency instead of its throughput.
    """
    k = V.Kernel()
    _preamble(k, vl, mode)
    ops = body or [V.alu("VADD", vd=d, va=0, vb=1, vc=1) for d in (2, 3, 4, 5)]
    k.seti(1, trips)
    k.emit(V.vloop(1, len(ops)), *ops, V.vhalt())
    return k


def fill_loop(words):
    """A VFILL of `words` memory words, and nothing else. Measures the read path."""
    k = V.Kernel()
    k.desc(0, SRC, V.dim(V.WORD_BYTES, words))
    return k.emit(V.vfill(0), V.vbar(), V.vhalt())


def drain_loop(words):
    """A VDRAIN of `words` memory words. Measures the write path."""
    k = V.Kernel()
    k.desc(4, DST, V.dim(V.WORD_BYTES, words))
    return k.emit(V.vdrain(4), V.vhalt())


def slope(s, node, make, lo, hi):
    """Cycles per unit, from the same kernel at two sizes. Fixed cost cancels."""
    c_lo = vector_cycles(s, node, make(lo))
    c_hi = vector_cycles(s, node, make(hi))
    return (c_hi - c_lo) / (hi - lo), c_lo, c_hi


def vector_report(s, node):
    print(f"\n=== VECTOR CORE ({node[0]},{node[1]}) -- exact, its own counter ===")
    print(
        f"  peak {VEC_ALUS} results/cycle in FLAT ({VEC_ALUS} ALUs), "
        f"{VEC_ALUS * CLOCK_HZ / 1e9:.1f} Gelem/s at {CLOCK_HZ / 1e6:.0f} MHz"
    )

    per_word, c_lo, c_hi = slope(s, node, fill_loop, 16, 128)
    print(
        f"\n  VFILL   {per_word:6.2f} cycles per 32-byte word   "
        f"({c_lo} @16, {c_hi} @128)"
    )
    print(f"          {32 / per_word * CLOCK_HZ / 1e9:.2f} GB/s from DRAM")

    per_word, c_lo, c_hi = slope(s, node, drain_loop, 16, 128)
    print(
        f"  VDRAIN  {per_word:6.2f} cycles per 32-byte word   "
        f"({c_lo} @16, {c_hi} @128)"
    )
    print(f"          {32 / per_word * CLOCK_HZ / 1e9:.2f} GB/s to DRAM")

    print("\n  VL sweep -- one VADD costs `nchunk` beats plus a fixed issue cost,")
    print(f"  and separating them says WHAT the gap to {VEC_ALUS}/cycle is made of.")
    print(
        f"  {'VL':>4s} {'chunks':>7s} {'cyc/op':>8s} {'elem/cyc':>9s} {'of peak':>8s}"
    )
    fit = []
    for vl in (32, 64, 96, 128):
        per_trip, _, _ = slope(s, node, lambda t, w=vl: alu_loop(t, vl=w), 8, 64)
        per_op = per_trip / 4
        fit.append((vl // V.LANES, per_op))
        print(
            f"  {vl:4d} {vl // V.LANES:7d} {per_op:8.2f} {vl / per_op:9.2f} "
            f"{100 * vl / per_op / VEC_ALUS:7.1f}%"
        )
    (c0, y0), (c1, y1) = fit[0], fit[-1]
    beat = (y1 - y0) / (c1 - c0)
    fixed = y0 - beat * c0
    print(
        f"  fit: {beat:.2f} cycles per 16-element beat + {fixed:.2f} fixed per "
        f"instruction"
    )
    print(
        f"  so the ceiling at VLMAX={V.VLMAX} is "
        f"{100 * 8 / (8 * beat + fixed):.1f}% of {VEC_ALUS}/cycle, by construction"
    )

    print(
        f"\n  {'mode':6s} {'ops/trip':>9s} {'cyc/trip':>9s} {'cyc/op':>8s} "
        f"{'elem/cyc':>9s} {'of peak':>8s}"
    )
    out = {}
    for name, mode, ops in vector_modes():
        try:
            per_trip, c_lo, c_hi = slope(
                s, node, lambda t, m=mode, o=ops: alu_loop(t, mode=m, body=o), 8, 64
            )
        except HardwareError as exc:
            print(f"  {name:6s} FAULT: {exc}")
            continue
        n_ops = len(ops)
        elems = 128 * n_ops / per_trip
        out[name] = elems
        print(
            f"  {name:6s} {n_ops:9d} {per_trip:9.2f} {per_trip / n_ops:8.2f} "
            f"{elems:9.2f} {100 * elems / VEC_ALUS:7.1f}%"
        )
    return out


def chain(n, base=2):
    """A group of `n` fused instructions: `v0+v1`, then `+1.0` n-1 times.

    Only the FIRST instruction of a group may name a vector register --
    `vec_core.v` S_GD raises F_VSRC on any later one that does -- so the rest
    take CHAIN and the constant register K1, which holds 1.0.
    """
    ops = [V.alu("VADD", vd=base, va=0, vb=1, vc=1)]
    for i in range(1, n):
        ops.append(
            V.alu(
                "VADD",
                vd=base + i,
                va=0,
                vb=1,
                vc=1,
                sa=V.SRC_C,
                sb=V.SRC_K,
                sc=V.SRC_K,
            )
        )
    return ops


def vector_modes():
    """FLAT, then the same arithmetic chained as D2 pairs and a D4 quad.

    `D2x2` is TWO groups per trip, landing on different registers. A group
    writes only its LAST destination, so one group per trip rewrites the same
    register every trip and stalls on `pend` -- which is a property of the
    measurement, not of D2, and the pair is what tells the two apart.
    """
    flat = [V.alu("VADD", vd=d, va=0, vb=1, vc=1) for d in (2, 3, 4, 5)]
    return [
        ("FLAT", V.FLAT, flat),
        ("D2", V.D2, chain(2)),
        ("D2x2", V.D2, chain(2, base=2) + chain(2, base=6)),
        ("D4", V.D4, chain(4)),
    ]


# --------------------------------------------------------------------------
# the clusters: wall clock, with the transport taken out
# --------------------------------------------------------------------------
def walk(link, cmds, timeout=30.0):
    """Execute a control program, counting the transactions it actually cost."""
    n = 0
    for pc, c in enumerate(cmds):
        if c.op == dev.OP_WR:
            link.write64(c.addr, c.data)
            n += 1
        elif c.op == dev.OP_POLL:
            deadline = time.monotonic() + timeout
            last = link.read64(c.addr)
            n += 1
            while (last & c.mask) != c.data:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"command {pc} held {last:#x}")
                last = link.read64(c.addr)
                n += 1
        elif c.op == dev.OP_DONE:
            return n
    return n


def cluster_report(s, shapes, reps, t_txn):
    print("\n=== CLUSTERS -- wall clock, transport subtracted ===")
    print(
        f"  peak {CLUSTER_PEAK / 1e9:.1f} GFLOP/s per cluster x "
        f"{s.board.ncl} = {s.board.ncl * CLUSTER_PEAK / 1e9:.1f} GFLOP/s"
    )
    print(
        f"  {'shape':14s} {'MFLOP':>8s} {'rnd':>4s} {'txn':>4s} "
        f"{'wall ms':>9s} {'transport':>10s} {'at peak us':>11s} "
        f"{'transport/compute':>18s}"
    )

    for m, k, n in shapes:
        why = s.check(m, k, n, PREQ)
        if why:
            print(f"  {m}x{k}x{n:<8} REFUSED: {'; '.join(why)}")
            continue
        prob = bench.build(
            m,
            k,
            n,
            0xC0FFEE,
            ncl=s.board.ncl,
            dist="lowrank",
            preq=PREQ,
            use=s.board.ncl,
            packing="spread",
            mesh_layout=s.board.layout,
            caps=s.board.caps(),
        )
        s.upload(prob)
        for prog in prob.programs:
            prog.upload(s.link)

        best = None
        for _ in range(reps):
            t0 = time.perf_counter()
            ntxn = sum(walk(s.link, p.cmds) for p in prob.programs)
            dt = time.perf_counter() - t0
            if best is None or dt < best[0]:
                best = (dt, ntxn)
        dt, ntxn = best

        flop = 2 * m * k * n
        transport = ntxn * t_txn
        at_peak = flop / (s.board.ncl * CLUSTER_PEAK)
        print(
            f"  {f'{m}x{k}x{n}':14s} {flop / 1e6:8.3f} "
            f"{len(prob.programs):4d} {ntxn:4d} {dt * 1e3:9.1f} "
            f"{transport * 1e3:9.1f}m {at_peak * 1e6:11.2f} "
            f"{transport / at_peak:17.0f}x"
        )

    print("\n  THERE IS NO CLUSTER NUMBER HERE, and the last column is why. The")
    print("  transport costs four to five ORDERS OF MAGNITUDE more than the work")
    print("  it is carrying, so wall clock minus transport is the difference of")
    print("  two large numbers whose noise is itself thousands of times the")
    print("  answer -- at 256x64x256 that residue came out NEGATIVE. Subtracting")
    print("  a per-transaction median from a wall clock cannot produce a figure")
    print("  for compute on this transport, at any shape this board will hold.")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--what", default="all", choices=("all", "vector", "cluster"))
    ap.add_argument("--shapes", default="128x64x128,256x64x256")
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--board")
    ap.add_argument("--transport", default="jtag")
    ap.add_argument("--node", help="x,y of the vector core to measure")
    args = ap.parse_args()

    shapes = [tuple(int(v) for v in sh.split("x")) for sh in args.shapes.split(",")]
    try:
        with Session(board=args.board, transport=args.transport) as s:
            s.handshake()
            s.reset()
            t_txn, lo, hi = transaction_cost(s)
            print("=== TRANSPORT ===")
            print(
                f"  JTAG register access  median {t_txn * 1e3:.3f} ms "
                f"(min {lo * 1e3:.3f}, max {hi * 1e3:.3f})"
            )
            print(f"  bulk write            {block_rate(s) / 1e3:.1f} kB/s")

            if args.what in ("all", "vector"):
                node = (
                    tuple(int(v) for v in args.node.split(","))
                    if args.node
                    else tuple(s.board.vectors[0])
                )
                vector_report(s, node)
            if args.what in ("all", "cluster"):
                cluster_report(s, shapes, args.reps, t_txn)
            return 0
    except TransportUnavailable as exc:
        print(f"no machine to run on: {exc}", file=sys.stderr)
        return 3
    except HardwareError as exc:
        print(f"the machine did not answer as expected:\n{exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
