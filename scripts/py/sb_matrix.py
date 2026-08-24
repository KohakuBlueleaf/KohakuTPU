#!/usr/bin/env python3
"""Every master/subordinate width pair of the station bus, in parallel.

    python scripts/py/sb_matrix.py            # the whole matrix
    python scripts/py/sb_matrix.py -j 16

One simulation PROCESS per combination, so a cell that hangs costs that cell and
nothing else, and the table says which shapes are legal rather than one shape
being called representative.

FW is 256, so MW sweeps below/equal/above it and SDW likewise; `sb_nsu`
instantiates an undefined module at SDW > FW, which is the RTL's own rule and the
only pair excluded here.
"""

import argparse
import pathlib
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(__file__).resolve().parents[2]
FW = 256
MWS = [32, 64, 128, 256, 512, 1024]
SDWS = [32, 64, 128, 256]

BIG = re.compile(
    r"LARGEST PASSING BURST: (\d+) beats of (\d+) bits \(legal max (\d+)\)"
)
VERDICT = re.compile(r"^\s*(PASS|FAIL)\s+(.*)$", re.MULTILINE)


def run(mw, sdw):
    tag = f"m{mw}_s{sdw}"
    out = ROOT / "build" / "sbmatrix" / tag
    out.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "py" / "xsim.py"),
        "sb_width",
        "-d",
        f"W_MW={mw}",
        "-d",
        f"W_SDW={sdw}",
        "--build-root",
        str(out),
        "--wall",
        "900",
    ]
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, check=False)
    txt = r.stdout + r.stderr
    (out / "run.log").write_text(txt, encoding="utf-8")
    big = BIG.search(txt)
    ver = VERDICT.findall(txt)
    return {
        "mw": mw,
        "sdw": sdw,
        "burst": int(big.group(1)) if big else None,
        "legal": int(big.group(3)) if big else None,
        "verdict": ver[-1][0] if ver else "NO-VERDICT",
        "detail": ver[-1][1].strip() if ver else txt.strip().splitlines()[-1:],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-j", type=int, default=12)
    a = ap.parse_args()

    jobs = [(mw, sdw) for mw in MWS for sdw in SDWS]
    print(f"{len(jobs)} combinations, {a.j} at a time, FW={FW}\n", flush=True)
    with ThreadPoolExecutor(max_workers=a.j) as pool:
        rows = list(pool.map(lambda t: run(*t), jobs))

    print(
        f"{'MW':>6} {'SDW':>6} {'vs FW':>6} {'vs SDW':>7} "
        f"{'burst':>6} {'legal':>6}  verdict"
    )
    bad = 0
    for r in sorted(rows, key=lambda x: (x["mw"], x["sdw"])):
        vfw = "<" if r["mw"] < FW else ("=" if r["mw"] == FW else ">")
        vsd = "<" if r["mw"] < r["sdw"] else ("=" if r["mw"] == r["sdw"] else ">")
        b = "-" if r["burst"] is None else r["burst"]
        lg = "-" if r["legal"] is None else r["legal"]
        short = r["burst"] is not None and r["burst"] < r["legal"]
        note = r["verdict"] + ("  BURST SHORT OF LEGAL MAX" if short else "")
        if r["verdict"] != "PASS" or short:
            bad += 1
        print(f"{r['mw']:>6} {r['sdw']:>6} {vfw:>6} {vsd:>7} {b:>6} {lg:>6}  {note}")
    print(f"\n{len(rows) - bad} of {len(rows)} clean")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
