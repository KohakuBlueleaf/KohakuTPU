#!/usr/bin/env python3
"""Collect every finished ooc_simd_pe run under build/dspshrink into one table.

    python scripts/py/simd_matrix.py [--dir build/dspshrink] [--csv out.csv]

A run directory is only reported when its log carries `@@@REC`; anything else is
listed as RUNNING or FAILED rather than silently omitted, because a missing row
in an area table reads as "that configuration is free".
"""

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]

# The tclargs of ooc_simd_pe.tcl, in order, so a row says what it was built as
# instead of what its directory name claims.
ORDER = [
    "den",
    "simd",
    "muls",
    "hsh",
    "hpm",
    "wbs",
    "per",
    "flt",
    "npt",
    "fln",
    "rmem",
    "dotd",
    "falu",
    "fsfu",
    "facc",
    "fcvt",
    "flat",
    "vprim",
    "sdir",
]

KEYS = [
    "lut",
    "lut_log",
    "lut_mem",
    "lut_dram",
    "lut_srl",
    "ff",
    "bram",
    "dsp",
    "ctrlsets",
]


def parse(log):
    """Return (cfg, rec, fmax, units) for one run.log, or None if unfinished."""
    text = log.read_text(errors="ignore")
    rec, fmax, units, cfg = {}, [], {}, {}

    # The tclargs echo Vivado prints on the command line it was launched with.
    m = re.search(r"-tclargs\s+(.*)", text)
    if m:
        vals = m.group(1).split()
        for k, v in zip(ORDER, vals):
            cfg[k] = v

    for ln in text.splitlines():
        if ln.startswith("@@@REC "):
            for kv in ln[len("@@@REC ") :].split():
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    rec[k] = v
        elif ln.startswith("@@@FMAX "):
            f = ln.split()
            if len(f) >= 4 and f[3] != "none":
                fmax.append((f[2], float(f[3])))
        elif ln.startswith("@@@ ") and "  LUT " in ln:
            # `@@@ <label>  LUT n  FF n  LUTRAM n  SRL n  BRAM n  MUXF n CARRY n`
            body = ln[4:]
            mm = re.match(
                r"(\S+)\s+LUT\s+(\d+)\s+FF\s+(\d+)\s+LUTRAM\s+(\d+)"
                r"\s+SRL\s+(\d+)\s+BRAM\s+(\d+)\s+MUXF\s+(\d+)"
                r"\s+CARRY\s+(\d+)",
                body,
            )
            if mm:
                units[mm.group(1)] = dict(
                    lut=int(mm.group(2)),
                    ff=int(mm.group(3)),
                    lutram=int(mm.group(4)),
                    srl=int(mm.group(5)),
                    bram=int(mm.group(6)),
                    muxf=int(mm.group(7)),
                    carry=int(mm.group(8)),
                )
    if not rec:
        return None if "@@@ top" not in text else ("INCOMPLETE", cfg, text)
    return cfg, rec, fmax, units


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="build/dspshrink")
    ap.add_argument("--csv")
    ap.add_argument(
        "--units", action="store_true", help="also print the per-instance breakdown"
    )
    a = ap.parse_args()

    base = ROOT / a.dir
    rows = []
    for d in sorted(base.iterdir()):
        log = d / "run.log"
        if not log.exists():
            print(f"{d.name}: no run.log", file=sys.stderr)
            continue
        got = parse(log)
        if got is None:
            print(f"{d.name}: EMPTY/FAILED", file=sys.stderr)
            continue
        if got[0] == "INCOMPLETE":
            print(f"{d.name}: RUNNING or DIED before @@@REC", file=sys.stderr)
            continue
        cfg, rec, fmax, units = got
        rows.append((d.name, cfg, rec, fmax, units))

    hdr = ["config"] + ORDER + KEYS + ["fmax"]
    print("\t".join(hdr))
    for name, cfg, rec, fmax, units in rows:
        best = min((f for _, f in fmax), default=0.0)
        out = (
            [name]
            + [cfg.get(k, "-") for k in ORDER]
            + [rec.get(k, "-") for k in KEYS]
            + [f"{best:.1f}"]
        )
        print("\t".join(out))
        if a.units:
            for k in sorted(units):
                u = units[k]
                print(
                    f"    {k:28s} LUT {u['lut']:6d} FF {u['ff']:6d} "
                    f"LUTRAM {u['lutram']:5d} SRL {u['srl']:4d} "
                    f"BRAM {u['bram']:3d} MUXF {u['muxf']:5d} "
                    f"CARRY {u['carry']:5d}"
                )

    if a.csv:
        p = pathlib.Path(a.csv)
        with p.open("w", encoding="utf-8") as fh:
            fh.write(",".join(hdr) + "\n")
            for name, cfg, rec, fmax, units in rows:
                best = min((f for _, f in fmax), default=0.0)
                fh.write(
                    ",".join(
                        [name]
                        + [cfg.get(k, "-") for k in ORDER]
                        + [rec.get(k, "-") for k in KEYS]
                        + [f"{best:.1f}"]
                    )
                    + "\n"
                )
        print(f"wrote {p}", file=sys.stderr)


if __name__ == "__main__":
    main()
