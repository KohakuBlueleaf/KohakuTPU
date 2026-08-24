"""Push one clock domain up until the arithmetic degrades, and report by how much.

    python scripts/py/fmax_ladder.py --mesh 0 --domain mat2x --to 900

There is no pass/fail here. A matmul is run at every step and scored against an
fp32 reference as p50/p90/p99/max RELATIVE error, because ~0.5% of elements
differ between two identical runs on this machine -- a max alone reports that
flicker, not the clock.

ONLY the types the domain drives are probed: mat2x drives MG, vec drives VC.
Probing a unit on a clock you are not moving measures nothing.

`set_isolated` has no nominal ceiling (only `set_profile` does), so this climbs
past the built rate deliberately. CLOCK DOWN HAPPENS BEFORE `Card` IS BUILT:
enumeration reads the mesh, and the mesh must never be read at speed.

A failed AXI transaction means JTAG is hung: this stops dead rather than
retrying, because a retry crashes the console and costs a reprogram.
"""

import argparse
import sys
import time

import numpy as np
from kohakuaccel.transport.jtag import JtagTransport
from kohakutpu.clock.card import CardClocks
from kohakutpu.clock.mmcm import ClockError

BOARD = "multimesh_v7"

#: Which unit type each wizard output actually clocks.
DRIVES = {"mat2x": "MG", "vec": "VC", "noc": None, "mag": None}


def score(got, want):
    """p50/p90/p99/max relative error against an fp32 reference."""
    scale = np.maximum(np.abs(want), np.abs(want).max() * 1e-3)
    r = np.abs(got - want) / scale
    return (
        float(np.percentile(r, 50)),
        float(np.percentile(r, 90)),
        float(np.percentile(r, 99)),
        float(r.max()),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mesh", type=int, default=0)
    ap.add_argument("--domain", default="mat2x")
    ap.add_argument("--to", type=float, default=900.0)
    ap.add_argument("--step", type=float, default=50.0)
    ap.add_argument("--m", type=int, default=64)
    ap.add_argument("--k", type=int, default=128)
    ap.add_argument("--n", type=int, default=128)
    ap.add_argument("--budget", type=float, default=50.0, help="seconds, hard stop")
    args = ap.parse_args()

    from kohakutpu.host import Card
    from kohakutpu.meshes import MeshGroup
    from kohakutpu.ops import matmul, rmsnorm

    want_type = DRIVES.get(args.domain)

    t = JtagTransport()
    clocks = CardClocks.from_board(t, BOARD)
    low = clocks.profile("low")
    clocks.set_all("low")

    card = Card.from_board(BOARD, transport=t, which=[args.mesh])
    mesh = card.mesh
    group = MeshGroup.open(card, [args.mesh])
    head = group[0]
    print(f"{mesh}\ndomain {args.domain} drives {want_type or 'every type'}\n")

    # THE WORKLOAD MUST DRIVE THE UNIT THE DOMAIN CLOCKS. A matmul runs on MG
    # and never touches VC, so laddering `vec` against it returns bit-identical
    # error at every step -- an idle unit, read as a pass (measured 2026-08-23).
    rng = np.random.default_rng(11)
    if want_type == "VC":
        x = rng.normal(0, 1.0, (args.m, args.k)).astype(np.float16)
        xf = x.astype(np.float32)
        want = xf / np.sqrt((xf * xf).mean(-1, keepdims=True) + 1e-5)
        run = lambda: rmsnorm(head.tensor(x)).numpy()
    else:
        a = rng.normal(0, 0.02, (args.m, args.k)).astype(np.float16)
        b = rng.normal(0, 1.00, (args.n, args.k)).astype(np.float16)
        want = a.astype(np.float32) @ b.astype(np.float32).T
        run = lambda: matmul(head.tensor(a), head.tensor(b), gm=8, gn=8, nk=2).numpy()

    wiz = clocks.mesh(args.mesh)
    held = {k: v for k, v in low.items() if k != args.domain}
    started = time.monotonic()
    mhz = low[args.domain]
    print(f"{'MHz':>7}  {'p50':>10} {'p90':>10} {'p99':>10} {'max':>10}   wall")

    while mhz <= args.to:
        if mhz != low[args.domain]:
            try:
                got_hz = wiz.set_isolated(args.domain, mhz, held)[args.domain]
            except ClockError as exc:
                print(f"{mhz:>7.1f}  unreachable: {exc}")
                mhz += args.step
                continue
        else:
            got_hz = mhz
        t0 = time.monotonic()
        try:
            y = run()
        except Exception as exc:  # noqa: BLE001
            print(f"{got_hz:>7.1f}  DIED {type(exc).__name__}: {exc}")
            break
        wall = time.monotonic() - t0
        p50, p90, p99, mx = score(y, want)
        print(
            f"{got_hz:>7.1f}  {p50:10.3e} {p90:10.3e} {p99:10.3e} {mx:10.3e}"
            f"   {wall:5.2f}s"
        )
        if time.monotonic() - started > args.budget:
            print(f"stopped on the {args.budget:g}s budget, not on a result")
            break
        mhz += args.step

    print(f"\nreturning mesh_{args.mesh} to low")
    wiz.set_profile(low)
    print(f"total {time.monotonic()-started:.2f}s, jtag calls {t.calls}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"\nABORTED: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(
            "If that was an AXI failure the JTAG path is hung -- REPROGRAM.",
            file=sys.stderr,
        )
        raise SystemExit(2)
