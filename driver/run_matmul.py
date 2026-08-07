"""Run one matmul on the simulated machine, batch style.

    python run_matmul.py [--m 16] [--n 16] [--k 32]

Every run elaborates the design from scratch, which is right for CI and wrong
for iterating: use `repl.py` or `server.py` for that, where one snapshot stays
warm and a shape costs ~1 s instead of ~16 s.

The driver builds everything -- the FP16 operand image and the control program
-- so a wrong program here produces a wrong answer there, which is what makes
this a test of the driver and not just of the hardware. Software never sees
MXFP7: the host uploads FP16 and the hardware quantises it, either on the way
into memory (``--online`` turns that off) or on every read.
"""

import argparse
import sys

from kohakutpu import bench, sim
from kohakutpu.sim import Session, SimError


def _glyph(m):
    """One character for a sample window's phase MASK.

    A mask, not a state: the phases overlap by design, so a glyph that had to
    pick one would hide the overlap. Upper case means the array was sweeping
    at the same time, which is the case worth spotting -- `F` is a fill hidden
    inside a sweep, `f` is one the array waited through.
    """
    f, g, d = m & 1, m & 2, m & 4
    if f and g and d:
        return "#"
    if g and f:
        return "F"
    if g and d:
        return "D"
    if g:
        return "g"
    if f:
        return "f"
    if d:
        return "d"
    return "."


def _pipeline(tl, width=104):
    """The measured phase timeline, one row per cluster."""
    lines = []
    for c in sorted(tl["cu"]):
        s = tl["cu"][c]
        if not s:
            continue
        step = max(1.0, len(s) / width)
        row = []
        i = 0
        while int(i * step) < len(s):
            lo = int(i * step)
            hi = max(lo + 1, int((i + 1) * step))
            m = 0
            for v in s[lo:hi]:
                m |= v
            row.append(_glyph(m))
            i += 1
        lines.append(f"    cu{c} |" + "".join(row) + "|")
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--m", type=int, default=16)
    ap.add_argument("--n", type=int, default=16)
    ap.add_argument("--k", type=int, default=32)
    ap.add_argument("--seed", type=int, default=0xC0FFEE)
    ap.add_argument("--rebuild", action="store_true", help="force re-elaboration")
    ap.add_argument("--quiet", action="store_true")
    # HOW MANY CLUSTERS THE MACHINE HAS. Compiled into the bench, so asking for
    # a different one re-elaborates. It was reachable only through KOHAKU_NCL,
    # which meant the four and eight-cluster references could not be run from
    # this command line at all.
    ap.add_argument(
        "--ncl",
        type=int,
        default=None,
        choices=bench.CLUSTER_COUNTS,
        help=f"clusters (default {bench.NCLUSTERS}, from KOHAKU_NCL)",
    )
    ap.add_argument(
        "--dist",
        choices=bench.DISTRIBUTIONS,
        default="lowrank",
        help="operand distribution (default lowrank, correlated like real data)",
    )
    # Which operands were quantised on the way into memory. The default is
    # both; the others exist because the ANSWER must not depend on the choice,
    # so running them is a direct check that the pre-quantised fetch path is
    # faithful rather than merely faster.
    ap.add_argument(
        "--preq",
        choices=("both", "none", "a", "b"),
        default="both",
        help="operands pre-quantised on upload (default both)",
    )
    args = ap.parse_args()

    preq = {"both": (True, True), "none": (False, False),
            "a": (True, False), "b": (False, True)}[args.preq]
    ncl = bench.NCLUSTERS if args.ncl is None else args.ncl

    why = bench.check(args.m, args.n, args.k, ncl, preq)
    if why:
        sys.exit("cannot run: " + "; ".join(why))

    session = Session()
    if args.rebuild:
        session.build(force=True)
    try:
        with session as s:
            res = s.run(
                args.m, args.n, args.k, args.seed,
                dist=args.dist, ncl=ncl, preq=preq,
            )
    except SimError as e:
        sys.exit(f"simulator error: {e}")

    shape = res["shape"]
    pa, pb = res["preq"]
    print(
        f"problem  C[{shape['m']},{shape['n']}] = "
        f"A[{shape['m']},{shape['k']}] @ B.T[{shape['n']},{shape['k']}]"
        f"  ({res['ncmd']} commands)"
        f"  pre-quant A={'y' if pa else 'n'} B={'y' if pb else 'n'}"
    )
    if not args.quiet:
        t = res["telemetry"]
        for g in res["dispatches"]:
            print(f"    -> cluster (x={g['cluster']['x']}, y={g['cluster']['y']})")
            for f in g["flits"]:
                print(f"         {f['text']}")
        print(f"    mem rd={t.get('mem_rd')} wr={t.get('mem_wr')}")
        # Only when a round actually failed. `mag_driver_tb` reports success as
        # ORCH-OK and says nothing about a pc, so printing "pc None code None"
        # on every healthy run was noise that looked like missing telemetry.
        if t.get("round_fail"):
            print(f"    {sim._round_fail_text(t)}")
    # RTL assertions are never suppressed: they name the cause, and losing one
    # turns a one-line diagnosis into an end-to-end bisect.
    for e in res["telemetry"].get("errors", []):
        print(f"    {e}")

    print()
    print(f"    C[0,0] hw {res['c_peek'][0][0]:.4f}   "
          f"fp64 {res['want_peek'][0][0]:.4f}")

    # Two DIFFERENT questions, and only the first one is ever a bug.
    #   err_hw     hardware against the MXFP7 model -- is the circuit right?
    #   err_total  hardware against FP64 -- what does the format cost?
    # Collapsing them into one number means a real datapath fault hides inside
    # the quantisation error that is supposed to be there.
    hw, tot = res["err_hw"]["rel"], res["err_total"]["rel"]
    # HOW MANY elements are bad, next to how bad the worst one is. `max` alone
    # cannot separate the two things that raise it: a single output that
    # cancelled against its own partial sums -- where FP22 accumulate rounding
    # is large relative to a near-zero result, and the model does not round --
    # from a lost or misplaced sub-tile, which is 16 elements at once. The
    # counts say which, and a bisect is the only other way to find out.
    print(f"    vs mxfp7 model  p50 {hw['p50']:.2e}  max {hw['max']:.2e}"
          f"  over1% {hw['over1_n']}/{hw['n']} ({hw['over1']:.2e})"
          f"  over10% {hw['over10_n']}")
    print(f"    vs fp64         p50 {tot['p50']:.2e}  p99 {tot['p99']:.2e}")

    # The bubble profile, on every run. A change that improves the end-to-end
    # number without moving the component counter it was aimed at has not been
    # understood, so both are printed together and neither is optional.
    p = res.get("profile")
    if p:
        occ, rate, fet = p["occupancy"], p["rate"], p["fetch"]
        print()
        print(f"    run {p['cycles']['run']} cyc   "
              f"{rate['macs_per_cycle']:.1f} MAC/cyc of {rate['peak_macs_per_cycle']}   "
              f"{rate['gflops']:.1f} GFLOP/s ({rate['utilisation'] * 100:.1f}%)")
        print("    occupancy  " + "  ".join(
            f"{ph} {occ[ph]['frac'] * 100:.1f}%" for ph in ("fill", "gemm", "drain", "idle")))
        # EVERY BUCKET OUT OF THE RESOURCE IT WAS COUNTED ON. The MAG and
        # fabric counters are summed over memory ports, so out of the run
        # alone they exceeded 100% and were not comparable with the CU ones
        # next to them; `*` marks the two that are tapped on cluster 0.
        def buckets(group, keys):
            return "  ".join(
                f"{key} {p[group][key]['frac'] * 100:.1f}%"
                + ("*" if p[group][key]["basis"] == "cluster0" else "")
                for key in keys
            )

        print(f"    mag        {buckets('mag', ('idle', 'qfill', 'qwait', 'qemit', 'wr'))}"
              f"   (of {p['mem_ports']} port-cycles)")
        print("    stalls     " + buckets(
            "stalls", ("in_bp", "out_bp", "cu_send", "cu_dry")))
        # WHY a component could not advance, not merely where it was. A stage
        # that got slower shows up here as a named reason.
        print("    why        " + buckets(
            "why", ("wack_nob", "wack_blk", "wslot_full", "rfill_dry",
                    "rwait_emit", "emit_bp")))
        print("    cu tails   " + buckets("why", ("cu_dwait", "cu_gwait"))
              + "   * cluster 0 only")
        print(f"    fetch      first {fet['latency']} cyc   "
              f"{fet['cycles_per_entry']:.1f} cyc/entry of floor {fet['floor']}   "
              f"C write amp {p['traffic']['c_write_amplification']:.2f}x")
        # The upload is where pre-quantisation is PAID -- once per tensor,
        # against once per read. Printed next to the run so the trade is
        # visible rather than argued.
        cy = p["cycles"]
        print(f"    upload     {cy['operand_beats']} beats   "
              f"{cy['operand_upload']} cyc   "
              f"round setup {cy['host_upload']} cyc")
        # Every resource's utilisation side by side. A rate alone cannot say
        # whether the machine is compute-bound, bus-bound or idle.
        # DO NOT ADD THESE. Every link here is full duplex -- AR/R and AW/W are
        # separate AXI channels, the NoC's two directions are separate wires --
        # so what binds is the largest, never the total.
        r = p["resource"]
        print("    resource   " + "  ".join(
            f"{k} {r[k] * 100:.1f}%"
            for k in ("flops", "mem_rd", "mem_wr", "noc_in", "noc_out")))

    # WHERE the phases were, not just how much of each. Two occupancy totals
    # that exceed the elapsed time prove overlap happened somewhere; only a
    # timeline shows where it did not, and a bubble is invisible in a sum.
    tl = res.get("timeline")
    if tl and tl.get("cu"):
        print()
        print(f"    pipeline   {tl['gap']} cycles/column   "
              "f fill  g gemm  d drain  UPPER = with gemm  . idle")
        for ln in _pipeline(tl):
            print(ln)

    # Two thresholds, from the measured physics rather than from taste.
    #
    #   vs the MXFP7 model -- FP22 accumulate plus the FP16 output conversion.
    #   docs/system.md s3 measures this at ~1.4e-4 mean; 1e-3 is ~7x margin and
    #   still an order of magnitude below anything a real fault produces (a
    #   dropped sub-tile or a mis-assembled entry moves p50, not just the tail).
    #
    #   vs FP64 -- what the format costs. docs/isa/memory.md s6.1 measures
    #   per-element p50 at 0.38%; 1e-2 is ~3x margin.
    #
    # The WORST element against the model is bounded too, and it has to be.
    # A p50 check alone passes a run where a handful of entries landed in the
    # wrong L1 slot: the median barely moves and the maximum explodes. That
    # happened -- a shared fetch delivered A entries to a cluster that was
    # executing its FILL B, p50 went 1.70e-4 -> 1.93e-4 and the worst element
    # went 1.0 -> 2.2e+02, and it reported PASS.
    #
    # Against FP64 the tail is deliberately NOT bounded: four K blocks with
    # independent scales cancel, so an output small against its intermediate
    # terms has a large relative error by arithmetic rather than by fault
    # (docs/system.md s3). Against the MXFP7 model that argument does not
    # apply -- both sides see the same cancellation -- so the tail there is
    # signal, not noise.
    # The worst element was bounded at 2.0, which was the 256-cube's value plus
    # margin -- and it is not a constant. Measured, same seed, both operands
    # pre-quantised:
    #
    #     256x256   max 1.00e+00   over10%  4 of  65,536
    #     256x512   max 2.43e+00   over10%  7 of 131,072
    #     256x1024  max 2.43e+00   over10% 19 of 262,144
    #
    # and 256x512 gives 2.43 on TWO clusters as well as on four, so it is the
    # shape rather than the concurrency. More outputs, more chances to draw one
    # that cancels against its own partial sums -- where FP22 accumulate
    # rounding is large relative to a near-zero result and the model does not
    # round at all.
    #
    # So bound the COUNT, which is what actually separates the two failures. A
    # mis-slotted entry or a lost sub-tile corrupts 16 contiguous elements AND
    # moves p50; the real fault this check exists for -- a shared fetch
    # delivering A entries into a cluster's FILL B -- took p50 1.70e-4 -> 1.93e-4
    # and the worst element to 2.2e+02. `max < 10` still catches that by 20x
    # while admitting a tail that is arithmetic, and `over10` catches a
    # corruption that hides inside the tail's magnitude.
    # SCORED IN ONE PLACE, `sim._verdict`, which the browser front end reads
    # too. They used to apply different rules to the same run: the page checked
    # only p99 against the model, so it could report PASS on a run this
    # command line failed -- and the count bound, the one that catches a
    # mis-slotted fetch, was the criterion it did not have.
    #
    # ONLY A BROKEN RUN FAILS. A check that GATES says the run did not happen:
    # no ORCH-OK, an RTL assertion, no finite output. Everything else is a
    # measurement shown against a reference figure, and missing one prints
    # `note` rather than `FAIL` -- the thresholds above were fitted to one
    # family of shapes, and failing a correct circuit on a shape they do not fit
    # teaches the reader to stop believing the verdict.
    v = res["verdict"]
    print()
    for c in v["checks"]:
        limit = "" if c["limit"] is None else f" (limit {c['limit']:g})"
        if c["pass"]:
            mark = "ok  "
        elif c.get("gates"):
            mark = "FAIL"
        else:
            mark = "note"
        print(f"    {mark} {c['name']:<22} {c['text']}{limit}")
    if v.get("advisories"):
        print(f"    {len(v['advisories'])} figure(s) outside their reference "
              f"— reported, not a failure")
    print("  PASS" if v["pass"] else "  FAIL")
    sys.exit(0 if v["pass"] else 1)


if __name__ == "__main__":
    main()
