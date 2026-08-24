#!/usr/bin/env python3
"""Launch ooc_simd_pe.tcl for one configuration, detached, into its own dir.

    python scripts/py/dsp_shrink_run.py <tag> [key=value ...]

Keys are the tclargs of scripts/tcl/ooc_simd_pe.tcl by name, so a row says
what it varies instead of counting positions in a fifteen-long list.

Detached on purpose: a synthesis run must not be a child of the session that
started it. `--wait` blocks for the collector's benefit and nothing else.
"""

import argparse
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
VIVADO = pathlib.Path(r"D:\Xilinx\Vivado\2024.2\bin\vivado.bat")
TCL = ROOT / "scripts" / "tcl" / "ooc_simd_pe.tcl"

# Positional order of ooc_simd_pe.tcl's tclargs, with the assembled 8+4
# FALU+FSFU configuration as the default row.
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
DEFAULT = {
    "den": 1,
    "simd": 8,
    "muls": 4,
    "hsh": 1,
    "hpm": 1,
    "wbs": 1,
    "per": "2.857",
    "flt": 1,
    "npt": 16,
    "fln": 4,
    "rmem": "distributed",
    "dotd": 1,
    "falu": 1,
    "fsfu": 1,
    "facc": 0,
    "fcvt": 0,
    "flat": "rebuilt",
    "vprim": "distributed",
    "sdir": "default",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tag")
    ap.add_argument("kv", nargs="*")
    ap.add_argument("--wait", action="store_true")
    a = ap.parse_args()

    cfg = dict(DEFAULT)
    for kv in a.kv:
        k, v = kv.split("=", 1)
        if k not in cfg:
            sys.exit(f"unknown key {k}; known: {' '.join(ORDER)}")
        cfg[k] = v

    wd = ROOT / "build" / "dspshrink" / a.tag
    wd.mkdir(parents=True, exist_ok=True)
    for stale in ("run.log", "run.jou"):
        p = wd / stale
        if p.exists():
            p.unlink()

    cmd = [
        str(VIVADO),
        "-mode",
        "batch",
        "-notrace",
        "-log",
        "run.log",
        "-journal",
        "run.jou",
        "-source",
        str(TCL),
        "-tclargs",
    ] + [str(cfg[k]) for k in ORDER]

    # CREATE_NO_WINDOW, never DETACHED_PROCESS: the latter means "do not inherit
    # the parent's console", so Windows gives the child A NEW CONSOLE WINDOW --
    # one per run, on the owner's desktop. The two flags are mutually exclusive.
    flags = 0
    if not a.wait and hasattr(subprocess, "CREATE_NO_WINDOW"):
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    p = subprocess.Popen(
        cmd,
        cwd=str(wd),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=flags,
    )
    print(f"{a.tag}: pid {p.pid} -> {wd / 'run.log'}")
    print("  " + " ".join(f"{k}={cfg[k]}" for k in ORDER))
    if a.wait:
        p.wait()
        print(f"{a.tag}: exit {p.returncode}")


if __name__ == "__main__":
    main()
