"""Print the configuration matrix as a plain table, one revision at a time.

    python tests/pe/tools/khs_matrix.py --csv <dir>/matrix.csv
    python tests/pe/tools/khs_matrix.py --csv <dir>/matrix.csv --rtl r3

Rows from different RTL revisions are NOT comparable, so they are printed in
separate blocks and never mixed into one ranking. Deltas are against the row
named by `--baseline` within the same revision and clock target.
"""

import argparse
import csv
import pathlib
import sys

COLS = [("config", 12, "s"), ("lut", 8, "d"), ("ff", 7, "d"),
        ("dsp", 5, "d"), ("bram", 6, "g"), ("fmax_mhz", 9, "f"),
        ("ctrlsets", 9, "d")]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--csv", required=True)
    ap.add_argument("--rtl", help="only this revision")
    ap.add_argument("--target", default="3.333")
    ap.add_argument("--baseline", default="s8")
    a = ap.parse_args()

    rows = list(csv.DictReader(pathlib.Path(a.csv).open(encoding="utf-8")))
    rows = [r for r in rows if r["target_ns"] == a.target]
    if a.rtl:
        rows = [r for r in rows if r["rtl"] == a.rtl]
    if not rows:
        print("  no rows at target %s%s" % (a.target,
                                            " for %s" % a.rtl if a.rtl else ""))
        return 1

    for rev in sorted({r["rtl"] for r in rows}):
        block = [r for r in rows if r["rtl"] == rev]
        base = next((r for r in block if r["config"] == a.baseline), None)
        print()
        print("  RTL %s, %s ns ask, %d configurations" % (rev, a.target, len(block)))
        print("  %-12s %8s %7s %5s %6s %9s %9s   %s"
              % ("config", "LUT", "FF", "DSP", "BRAM", "Fmax MHz", "ctrlsets",
                 "vs " + a.baseline))
        print("  " + "-" * 88)
        for r in block:
            d = ""
            if base and r is not base:
                try:
                    d = "%+d LUT  %+.1f MHz" % (
                        int(float(r["lut"])) - int(float(base["lut"])),
                        float(r["fmax_mhz"]) - float(base["fmax_mhz"]))
                except (ValueError, KeyError):
                    d = ""
            print("  %-12s %8d %7d %5d %6g %9.1f %9s   %s"
                  % (r["config"], int(float(r["lut"])), int(float(r["ff"])),
                     int(float(r["dsp"])), float(r["bram"]),
                     float(r["fmax_mhz"]), r["ctrlsets"], d))
        print("  " + "-" * 88)
        bad = [r["config"] for r in block if r["gates"] != "PASS"]
        if bad:
            print("  NOT GATED AS ITSELF: %s" % ", ".join(bad))
    return 0


if __name__ == "__main__":
    sys.exit(main())
