"""Generate, simulate and report the DSP workload suite in one command.

    python tests/pe/tools/rv_simd_run.py
    python tests/pe/tools/rv_simd_run.py --csv <dir>/phase0.csv

Prints a table of kernel cycles, kernel instructions and CPI, with the timing
bracket's own cost (`nullkern`) subtracted from every row -- so a number here is
the kernel and nothing else.

The CSV path has no default: this file is tracked and where the research lives
is not its business.
"""

import argparse
import csv
import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
TOOLS = ROOT / "tests" / "pe" / "tools"
XSIM = ROOT / "scripts" / "py" / "xsim.py"
INDEX = ROOT / "tests" / "pe" / "build" / "simd" / "index.txt"

ROW = re.compile(r"@@@ DSP (\d+) cycles (\d+) kinstr (\d+) retired (\d+) "
                 r"total (\d+) (\S+)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--csv", help="append the rows here as well")
    ap.add_argument("--rtl", default="scalar-base",
                    help="the RTL revision these rows were measured on")
    ap.add_argument("--simd", type=int, default=8, choices=(2, 4, 8))
    ap.add_argument("--wall", type=float, default=900.0)
    ap.add_argument("--define", "-d", action="append", default=[])
    ap.add_argument("--no-gen", action="store_true")
    a = ap.parse_args()

    if not a.no_gen:
        if subprocess.run([sys.executable, str(TOOLS / "rv_simd_gen.py"),
                           "--simd", str(a.simd)],
                          cwd=str(ROOT), check=False).returncode:
            return 1

    names = {}
    notes = {}
    for ln in INDEX.read_text().splitlines():
        f = ln.split("\t")
        names[int(f[0])] = f[1]
        notes[int(f[0])] = f[6]

    # The BENCH's width and the VECTORS' width must be the same number: a
    # kernel generated for eight lanes faults on a four-lane build rather than
    # running slowly.
    cmd = [sys.executable, str(XSIM), "rv_dsp", "--wall", str(a.wall),
           "-d", "RV_SIMD_LANES=%d" % a.simd]
    for d in a.define:
        cmd += ["-d", d]
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                       check=False)
    out = r.stdout
    rows = []
    for m in ROW.finditer(out):
        n, cyc, kin, ret, tot, verdict = m.groups()
        rows.append(dict(n=int(n), name=names[int(n)], cycles=int(cyc),
                         kinstr=int(kin), retired=int(ret), total=int(tot),
                         verdict=verdict, note=notes[int(n)]))

    passed = "PASS --" in out and "FAIL" not in out
    if not rows:
        print(out[-4000:])
        print("  NO ROWS -- the bench produced no @@@ DSP lines")
        return 1

    # nullkern is the bracket itself: two CTL_CYCLE reads and the instructions
    # between them. Subtracting it is what makes a row the kernel alone.
    base = next((x["cycles"] for x in rows if x["name"] == "nullkern"), 0)

    for x in rows:
        x["net_cycles"] = x["cycles"] - base
        x["cpi"] = round((x["net_cycles"] / x["kinstr"]) if x["kinstr"] else 0.0, 3)

    print()
    print("  %-14s %9s %9s %6s  %s" % ("kernel", "cycles", "instr", "CPI", "note"))
    print("  " + "-" * 76)
    for x in rows:
        print("  %-14s %9d %9d %6.3f  %s%s"
              % (x["name"], x["net_cycles"], x["kinstr"], x["cpi"], x["note"],
                 "" if x["verdict"] == "ok" else "   <-- " + x["verdict"]))
    print("  " + "-" * 76)
    print("  bracket overhead subtracted from every row: %d cycles" % base)

    # THE SPECIALIZATION FRONTIER. A `_v` kernel against its scalar twin, and
    # where the twin has a `_nomul` form, against that too: the second ratio is
    # what SIMD width buys once a multiplier is assumed, and the difference
    # between the two is what the multiplier alone buys.
    by = {x["name"]: x for x in rows}
    pairs = [(n, n[:-2]) for n in by if n.endswith("_v") and n[:-2] in by]
    if pairs:
        print()
        print("  %-14s %10s %10s %9s  %s"
              % ("vector kernel", "scalar", "vector", "speedup", "against"))
        print("  " + "-" * 76)
        for v, s in sorted(pairs):
            for base_name in ([s, s + "_nomul"] if (s + "_nomul") in by else [s]):
                sc, vc = by[base_name]["net_cycles"], by[v]["net_cycles"]
                print("  %-14s %10d %10d %8.1fx  %s"
                      % (v, sc, vc, (sc / vc) if vc else 0.0, base_name))
        print("  " + "-" * 76)
    print("  verdict: %s" % ("PASS" if passed else "FAIL"))

    if a.csv:
        p = pathlib.Path(a.csv)
        p.parent.mkdir(parents=True, exist_ok=True)
        fields = ["rtl", "config", "simd", "name", "net_cycles", "kinstr",
                  "cpi", "raw_cycles", "retired", "total_cycles", "verdict",
                  "note"]
        new = not p.exists()
        cfg = " ".join(a.define) or "default"
        with p.open("a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            if new:
                w.writeheader()
            for x in rows:
                w.writerow({"rtl": a.rtl, "config": cfg, "simd": a.simd,
                            "name": x["name"],
                            "net_cycles": x["net_cycles"], "kinstr": x["kinstr"],
                            "cpi": x["cpi"], "raw_cycles": x["cycles"],
                            "retired": x["retired"], "total_cycles": x["total"],
                            "verdict": x["verdict"], "note": x["note"]})
        print("  appended %d rows to %s" % (len(rows), p))

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
