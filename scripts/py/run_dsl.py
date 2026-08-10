"""Run DSL-generated machine code on the simulated machine.

    python run_dsl.py [--m 64] [--k 64] [--n 64]

The whole compiler, end to end and on real hardware: a DSL kernel is traced to
a level 1 graph, scheduled into level 2 bands, lowered to a level 3 program,
encoded to flits, and those flits -- not the planner's -- are what the cluster
executes in xsim. The answer is scored against fp64 and against the MXFP7
model exactly as `run_matmul.py` scores the planner's.

`run_matmul.py` is the same simulation driven by `ktpu.hw.bench`. Running both
on one shape separates a compiler fault from a hardware one: same operands,
same control program, same RTL, and the only difference is who emitted the
instructions.
"""

import argparse
import sys

from ktpu.hw import fromdsl
from ktpu.hw.sim import Session, SimError


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--m", type=int, default=64)
    ap.add_argument("--k", type=int, default=64)
    ap.add_argument("--n", type=int, default=64)
    ap.add_argument("--seed", type=int, default=0xC0FFEE)
    ap.add_argument("--rebuild", action="store_true", help="force re-elaboration")
    args = ap.parse_args()

    prog = fromdsl.program(args.m, args.k, args.n)
    print(
        f"level 3   {len(prog.insts)} instructions, {len(prog.memory.regions)} regions"
    )
    for inst in prog.insts:
        print(f"    {inst.op.name:<8} {inst.fields}")

    session = Session()
    if args.rebuild:
        session.build(force=True)
    try:
        with session as s:
            res = s.run(
                args.m,
                args.k,
                args.n,
                args.seed,
                ncl=1,
                transform=lambda p: fromdsl.restage(p, args.m, args.k, args.n),
            )
    except (SimError, ValueError) as e:
        sys.exit(f"cannot run: {e}")

    print()
    print(
        f"    C[0,0] hw {res['c_peek'][0][0]:.4f}   fp64 {res['want_peek'][0][0]:.4f}"
    )
    hw, tot = res["err_hw"]["rel"], res["err_total"]["rel"]
    print(
        f"    vs mxfp7 model  p50 {hw['p50']:.2e}  max {hw['max']:.2e}"
        f"  over1% {hw['over1_n']}/{hw['n']}"
    )
    print(f"    vs fp64         p50 {tot['p50']:.2e}  p99 {tot['p99']:.2e}")

    v = res["verdict"]
    print()
    for c in v["checks"]:
        limit = "" if c["limit"] is None else f" (limit {c['limit']:g})"
        mark = "ok  " if c["pass"] else ("FAIL" if c.get("gates") else "note")
        print(f"    {mark} {c['name']:<22} {c['text']}{limit}")
    print("  PASS" if v["pass"] else "  FAIL")
    sys.exit(0 if v["pass"] else 1)


if __name__ == "__main__":
    main()
