"""Interactive matmul on the simulated machine.

    python repl.py

Type a shape, get a matmul. The design is elaborated once at startup and the
session stays warm, so each shape after the first costs about a second rather
than about sixteen -- see ktpu.hw.sim.

    > 16 32 16          M K N
    > 32 32 32 7        M K N seed
    > c                 show the last result's C matrix
    > q                 quit

SHAPES ARE M, K, N -- the contracted dimension in the middle, as everywhere
else in this driver. Three bare integers: a transposition is a different GEMM
that runs perfectly well and answers a question nobody asked.
"""

import sys

import numpy as np
from ktpu.hw import bench, sim
from ktpu.hw.sim import Session, SimError

HELP = "  <M> <K> <N> [seed] to run   ·   c for the last C   ·   q to quit"


def show_matrix(name, mat, limit=8, shape=None):
    """A corner of `mat`, labelled with the FULL problem shape.

    `mat` is already only a corner of the answer, so its own dimensions would
    describe the excerpt rather than the matmul -- `shape` is what the run
    actually computed.
    """
    rows, cols = (shape["m"], shape["n"]) if shape else mat.shape
    r, c = min(mat.shape[0], limit), min(mat.shape[1], limit)
    print(
        f"  {name}  [{rows}x{cols}]"
        + ("  (top-left corner)" if (r, c) != (rows, cols) else "")
    )
    for i in range(r):
        print(
            "    "
            + "  ".join(f"{mat[i][j]:8.3f}" for j in range(c))
            + ("   ..." if c < cols else "")
        )
    if r < rows:
        print("    ...")


def report(res):
    s = res["shape"]
    t = res["telemetry"]

    print()
    for g in res["dispatches"]:
        print(f"  -> cluster (x={g['cluster']['x']}, y={g['cluster']['y']})")
        for f in g["flits"]:
            print(f"       {f['text']}")
    print(
        f"  {res['ncmd']} commands · "
        f"mem {t.get('mem_rd')} reads / {t.get('mem_wr')} writes"
    )
    if t.get("round_fail"):
        print(f"  {sim._round_fail_text(t)}")
    for e in t.get("errors", []):
        print(f"  {e}")
    print()
    # The corner only: a 512x1024 answer is half a million elements.
    show_matrix("hardware C", np.array(res["c_peek"]), shape=s)
    print()
    show_matrix("fp64 reference", np.array(res["want_peek"]), shape=s)
    print()

    # Scored by `sim._verdict`, the same rule run_matmul.py applies, rather
    # than by a threshold of this file's own.
    hw = res["err_hw"]["rel"]
    v = res["verdict"]
    print(
        f"  C[{s['m']},{s['n']}] = A[{s['m']},{s['k']}] @ B.T[{s['n']},{s['k']}]   "
        f"{res['seconds']}s   {res['ncl']} cluster(s)"
    )
    print(
        f"  vs mxfp7 model  p50 {hw['p50']:.2e}  worst {hw['max']:.2e}  "
        f"over10% {hw['over10_n']}/{hw['n']}   "
        f"{'PASS' if v['pass'] else 'FAIL'}"
    )
    if not v["pass"]:
        for c in v["checks"]:
            if not c["pass"]:
                print(f"    FAIL {c['name']}: {c['text']}")
    print()


def main():
    print("  elaborating the design (once) ...", flush=True)
    last = None
    with Session() as s:
        print("  simulator warm.")
        print(HELP)
        while True:
            try:
                line = input("  > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not line:
                continue
            if line in ("q", "quit", "exit"):
                return
            if line in ("h", "help", "?"):
                print(HELP)
                continue
            if line == "c":
                if last is None:
                    print("  nothing run yet")
                else:
                    show_matrix(
                        "hardware C",
                        np.array(last["c_peek"]),
                        limit=64,
                        shape=last["shape"],
                    )
                continue

            parts = line.split()
            try:
                m, k, n = (int(x) for x in parts[:3])
                seed = int(parts[3]) if len(parts) > 3 else 0xC0FFEE
            except (ValueError, IndexError):
                print(f"  need three numbers.{HELP}")
                continue

            why = bench.check(m, k, n)
            if why:
                for w in why:
                    print(f"  cannot run: {w}")
                continue
            # Said BEFORE the run, because afterwards nothing says it: a
            # saturated FP16 output scores as a pass.
            for w in bench.preview(m, k, n)["warnings"]:
                print(f"  note: {w}")
            try:
                last = s.run(m, k, n, seed)
            except SimError as e:
                print(f"  simulator error: {e}")
                continue
            report(last)


if __name__ == "__main__":
    sys.exit(main())
