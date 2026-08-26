#!/usr/bin/env python3
"""Price every configurable width on both PEs, in one run.

    python scripts/py/khs_sweep.py --list
    python scripts/py/khs_sweep.py --all --jobs 12
    python scripts/py/khs_sweep.py ilanes simt_flanes

EVERY WIDTH GETS AT LEAST TWO POINTS, because one measurement is a number and
two are a slope. A width whose curve is known to be non-linear gets more, and
the sweep that needs them says why.

A point is a MARGINAL cost: the difference between two rows that differ in ONE
generic. Never divide a total by a count -- that charges the tier's fixed
overhead to the units and invents a defect.

EVERY ROW PRINTS ITS WHOLE CONFIGURATION. A LUT figure quoted without one is not
a measurement: the reader fills the gaps with zeros and prices a bare core
against a fully-featured one.
"""

import argparse
import itertools
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = pathlib.Path(__file__).resolve().parents[2]
VIVADO = pathlib.Path(r"D:\Xilinx\Vivado\2024.2\bin\vivado.bat")
WORK = ROOT / "build" / "sweep"

#: The SIMD PE's positionals, in the order ooc_simd_pe.tcl reads them.
#: Positions 2 and 3 are ilanes and red; 4 and 11 are reserved.
SIMD_BASE = [
    "1",  # 0  SIMD_EN
    "8",  # 1  SIMD_LANES
    "8",  # 2  ILANES
    "1",  # 3  RED_UNITS
    "0",  # 4  reserved
    "0",  # 5  WB_STAGE
    "3.333",  # 6  period
    "1",  # 7  float on
    "16",  # 8  NPART
    "8",  # 9  FLOAT_LANES
    "distributed",  # 10 RECV_MEM
    "0",  # 11 reserved
    "1",  # 12 HAS_FALU
    "1",  # 13 FSFU_UNITS
    "0",  # 14 HAS_FACC
    "0",  # 15 FCVT_UNITS
    "rebuilt",  # 16 flatten
    "distributed",  # 17 VREG_PRIM
    "default",  # 18 directive
    "8",  # 19 PERM_UNITS
    "2",  # 20 NACC
    "8",  # 21 VREGS
    "8",  # 22 SHIFT_UNITS
]
SIMD_POS = {
    "en": 0,
    "simd": 1,
    "ilanes": 2,
    "red": 3,
    "wb": 5,
    "period": 6,
    "float": 7,
    "npart": 8,
    "flanes": 9,
    "recvmem": 10,
    "falu": 12,
    "fsfu": 13,
    "facc": 14,
    "fcvt": 15,
    "flatten": 16,
    "vregprim": 17,
    "directive": 18,
    "permu": 19,
    "nacc": 20,
    "vregs": 21,
    "shiftu": 22,
}

#: The SIMT PE's positionals, in the order ooc_simt_pe.tcl reads them.
SIMT_BASE = [
    "kht_pe",
    "8",
    "16",
    "1",
    "1",
    "3.333",
    "block",
    "1",
    "rebuilt",
    "1",
    "8",
    "1",
    "1",
    "8",
    "block",
    "16",
    "512",
    "2048",
    "2048",
    "128",
    "0",
    "8",
    "-1",
    "-1",
]
SIMT_POS = {
    "top": 0,
    "lanes": 1,
    "waves": 2,
    "mask": 3,
    "ipdom": 4,
    "period": 5,
    "vregprim": 6,
    "shfl": 7,
    "flatten": 8,
    "fpu": 9,
    "flanes": 10,
    "fsfu": 11,
    "ldsbank": 12,
    "ipdomd": 13,
    "memprim": 14,
    "instdepth": 15,
    "recvdepth": 16,
    "imemwords": 17,
    "spadwords": 18,
    "l1lines": 19,
    "reserved20": 20,
    "reserved21": 21,
    "shflu": 22,
    "ldsb": 23,
}

CORES = {
    "simd": ("scripts/tcl/ooc_simd_pe.tcl", SIMD_BASE, SIMD_POS),
    "simt": ("scripts/tcl/ooc_simt_pe.tcl", SIMT_BASE, SIMT_POS),
}

#: name -> (core, why these points, [ {knob: value} ... ])
SWEEPS = {
    # ---- the two bases every marginal figure is a difference above ----
    "base_simd": (
        "simd",
        "The RV32IM PE alone, then with the vector unit, then with float.",
        [{"en": "0"}, {"en": "1", "float": "0", "flanes": "0", "fsfu": "0"}, {}],
    ),
    "base_simt": (
        "simt",
        "The threads and the divergence machine, then with the float tier.",
        [{"flanes": "0", "fsfu": "0"}, {}],
    ),
    # ---- the true empty machine: every compute width at zero ----
    "empty": (
        "simd",
        (
            "EVERY WIDTH AT ZERO -- a vector register file, the scratchpad and "
            "the 256-bit load/store path, and nothing that computes. This is "
            "the base a per-unit figure is a difference above."
        ),
        [
            {
                "ilanes": "0",
                "shiftu": "0",
                "permu": "0",
                "red": "0",
                "float": "0",
                "flanes": "0",
                "fsfu": "0",
                "facc": "0",
            },
            {},
        ],
    ),
    # ---- SIMD widths ----
    "ilanes": (
        "simd",
        "The integer tier. Three points because it is meant to extrapolate.",
        [{"ilanes": "0"}, {"ilanes": "2"}, {"ilanes": "8"}],
    ),
    "shiftu": (
        "simd",
        "The packed shifter, including its not-built point.",
        [{"shiftu": "0"}, {"shiftu": "2"}, {"shiftu": "8"}],
    ),
    "permu": (
        "simd",
        (
            "FIVE POINTS. The permute is the one width kept on a float-oriented "
            "part, its curve is known to be strongly non-linear, and its COUNT "
            "is chosen from this sweep rather than assumed."
        ),
        [
            {"permu": "0"},
            {"permu": "1"},
            {"permu": "2"},
            {"permu": "4"},
            {"permu": "8"},
        ],
    ),
    "red": (
        "simd",
        "The reduce trees; a count whose only values are 0 and 1.",
        [{"red": "0"}, {"red": "1"}],
    ),
    "flanes": (
        "simd",
        "The float tier's width. Maximum is SIMD; there is no 2*SIMD.",
        [
            {"flanes": "0", "fsfu": "0"},
            {"flanes": "2"},
            {"flanes": "4"},
            {"flanes": "8"},
        ],
    ),
    "fsfu": (
        "simd",
        (
            "The seed ratio. Real GPUs provision 1:4 to 1:8 of the float units, "
            "so at 8 lanes the architectural answer is 1 or 2 -- but the walk "
            "folds at FSFU == FLANES, which can make full rate the cheapest."
        ),
        [{"fsfu": "0"}, {"fsfu": "1"}, {"fsfu": "2"}, {"fsfu": "8"}],
    ),
    "shround": (
        "simd",
        "vsrari's round adder, one SWAR add per lane inside the shifter.",
        [{"xgen": "SIMD_SHROUND:0"}, {}],
    ),
    # ---- the float GROUPS, which the base leaves off ----
    "facc": (
        "simd",
        (
            "The float accumulator. THE BASE HAS IT OFF, so no other row in this "
            "sweep carries its cost -- this is the only place it is priced."
        ),
        [{"facc": "0"}, {"facc": "1"}],
    ),
    "fcvt": (
        "simd",
        "int32 <-> binary32 converter units, per pass.",
        [{"fcvt": "0"}, {"fcvt": "1"}, {"fcvt": "2"}, {"fcvt": "4"}, {"fcvt": "8"}],
    ),
    "nacc": (
        "simd",
        (
            "Accumulator banks. facc=1 ON EVERY ROW: with the accumulator off "
            "the banks do not exist and the sweep would report 0 for a knob it "
            "never built."
        ),
        [
            {"facc": "1", "nacc": "1"},
            {"facc": "1", "nacc": "2"},
            {"facc": "1", "nacc": "4"},
        ],
    ),
    "npart": (
        "simd",
        "Accumulator partials, likewise meaningless without facc=1.",
        [{"facc": "1", "npart": "8"}, {"facc": "1", "npart": "16"}],
    ),
    # ---- storage: a LUT-for-BRAM trade, not a free choice ----
    "vregprim": (
        "simd",
        "The vector file in LUTRAM against BRAM.",
        [{"vregprim": "distributed"}, {"vregprim": "block"}],
    ),
    "recvmem": (
        "simd",
        "The receive queue's storage, the same trade one level out.",
        [{"recvmem": "distributed"}, {"recvmem": "block"}],
    ),
    "vregs": (
        "simd",
        "Vector register file depth.",
        [{"vregs": "8"}, {"vregs": "16"}],
    ),
    "wb": (
        "simd",
        "The extra writeback stage: a timing knob that costs area.",
        [{"wb": "0"}, {"wb": "1"}],
    ),
    # ---- NOT a marginal: several generics move together ----
    "simd_width": (
        "simd",
        (
            "THE VECTOR WIDTH ITSELF, and every compute width tracks it -- a "
            "width cannot exceed the vector, so this row changes SIX generics "
            "at once. Read it as three SHAPES, never as a marginal."
        ),
        [
            {
                "simd": "2",
                "ilanes": "2",
                "shiftu": "2",
                "permu": "2",
                "flanes": "2",
                "npart": "4",
            },
            {
                "simd": "4",
                "ilanes": "4",
                "shiftu": "4",
                "permu": "4",
                "flanes": "4",
                "npart": "8",
            },
            {"simd": "8"},
        ],
    ),
    # ---- the shapes actually proposed, measured rather than estimated ----
    "shape_simd": (
        "simd",
        (
            "THE PROPOSED GPU SIMD PE: float at full width, one seed per eight, "
            "one converter, a narrow integer tier, no packed shifter, two "
            "permute units. Measured because the additive estimate over-"
            "predicts a stripped build."
        ),
        [{"ilanes": "2", "shiftu": "0", "permu": "2", "fcvt": "1"}],
    ),
    "shape_simt": (
        "simt",
        "THE PROPOSED GPU SIMT PE: full float, two shuffle units, two LDS banks.",
        [{"shflu": "2", "ldsb": "2"}],
    ),
    # The integer tier as a LADDER against a fixed float shape; every feature
    # not named is 0, so the number is the machine, not a default nobody chose.
    "tier_ilanes": (
        "simd",
        (
            "shiftu 1, permu 1, red 1, flanes 8, fsfu 1, fcvt 1, facc 0 -- the "
            "integer lane count is the only thing that moves."
        ),
        [
            {
                "ilanes": i,
                "shiftu": "1",
                "permu": "1",
                "red": "1",
                "flanes": "8",
                "fsfu": "1",
                "fcvt": "1",
                "facc": "0",
            }
            for i in ("8", "4", "2", "1")
        ],
    ),
    # ---- SIMT widths ----
    "simt_flanes": (
        "simt",
        "The SIMT float width, on the same tree as every SIMD row.",
        [
            {"flanes": "0", "fsfu": "0"},
            {"flanes": "2"},
            {"flanes": "4"},
            {"flanes": "8"},
        ],
    ),
    "simt_fsfu": (
        "simt",
        "The SIMT seed ratio; its walk is a write enable, not a placement mux.",
        [{"fsfu": "0"}, {"fsfu": "1"}, {"fsfu": "2"}, {"fsfu": "8"}],
    ),
    "simt_waves": (
        "simt",
        "Occupancy against storage -- the one SIMT knob that is not a tier.",
        [{"waves": "8"}, {"waves": "16"}],
    ),
    # These two read `0 = full rate` until kht_unit was corrected, so shflu=0
    # and shflu=8 were the SAME build and the sweep measured a span of +0 LUT.
    "simt_shflu": (
        "simt",
        "The subgroup butterfly as a width, including its not-built point.",
        [
            {"shflu": "0"},
            {"shflu": "1"},
            {"shflu": "2"},
            {"shflu": "4"},
            {"shflu": "8"},
        ],
    ),
    "simt_ldsb": (
        "simt",
        "The banked shared memory as a width, including its not-built point.",
        [
            {"ldsb": "0"},
            {"ldsb": "1"},
            {"ldsb": "2"},
            {"ldsb": "4"},
            {"ldsb": "8"},
        ],
    ),
}

# ANCHORED, NOT STYLE: Vivado echoes the Tcl it sources, so ooc_class.tcl's own
# `set out "@@@REC tag=$tag $cfg"` is in the log above the real line. Unanchored
# it matched that echo and reported twelve good synths as FAILED.
REC = re.compile(r"^@@@REC\s+(.*)$", re.MULTILINE)
# `@@@FMAX <tag> <clock> <mhz>` -- THREE fields before the number. Skipping only
# one matched the clock NAME, never digits, so every row reported Fmax as `?`.
FMAX = re.compile(r"^@@@FMAX\s+\S+\s+\S+\s+([\d.]+)", re.MULTILINE)


def free_gb():
    """Available physical memory in GB, or None if it cannot be read."""
    if os.name == "nt":
        import ctypes

        class MS(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        st = MS()
        st.dwLength = ctypes.sizeof(MS)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return st.ullAvailPhys / (1024**3)
        return None
    try:
        return (os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")) / (1024**3)
    except (ValueError, OSError, AttributeError):
        return None


def rec_fields(out: str) -> dict:
    # The LAST match with a `lut=` in it: the echo cannot have one.
    for m in reversed(REC.findall(out)):
        d = {}
        for kv in m.split():
            if "=" in kv:
                k, v = kv.split("=", 1)
                d[k] = v
        if "lut" in d:
            return d
    return {}


def describe(argv: list, pos: dict) -> str:
    inv = {i: n for n, i in pos.items()}
    return " ".join(f"{inv.get(i, f'arg{i}')}={v}" for i, v in enumerate(argv))


def argv_for(point: dict, base: list, pos: dict) -> list:
    a = list(base)
    for k, v in point.items():
        if k == "xgen":
            continue
        if k not in pos:
            sys.exit(f"unknown knob {k!r}; known: {' '.join(sorted(pos))}")
        a[pos[k]] = v
    if "xgen" in point:
        a.append(point["xgen"])
    return a


def run(argv: list, script: str, tag: str) -> dict:
    # A PER-JOB WORKING DIRECTORY. Concurrent Vivado runs sharing one cwd share
    # `.Xil`, and the collision presents as "couldn't read file" on the install
    # tree -- a failure that reads like a missing source rather than a race.
    work = WORK / tag
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(VIVADO),
        "-mode",
        "batch",
        "-nojournal",
        "-nolog",
        "-source",
        str(ROOT / script),
        "-tclargs",
    ] + argv
    t = time.monotonic()
    # AREA ONLY: skips the timing forensics whose path objects, times twelve
    # concurrent jobs, filled 111 GB and stalled the machine.
    env = dict(os.environ, KOHAKU_OOC_LEAN="1")
    p = subprocess.run(
        cmd, cwd=str(work), capture_output=True, text=True, check=False, env=env
    )
    out = (p.stdout or "") + (p.stderr or "")
    # In memory it dies with the process and the estimator has nothing to fit
    # against; `run.log` is also the name simd_matrix.py already looks for.
    (work / "run.log").write_text(out, encoding="utf-8", errors="replace")
    d = rec_fields(out)
    # The WORST clock, not the first one printed: a PE with a fast clock and a
    # slow one is limited by the slow one.
    fs = [float(x) for x in FMAX.findall(out)]

    def num(k):
        try:
            return int(float(d[k]))
        except (KeyError, ValueError):
            return None

    return {
        "lut": num("lut"),
        "ff": num("ff"),
        "dsp": num("dsp"),
        "bram": d.get("bram", "?"),
        "fmax": f"{min(fs):.1f}" if fs else "?",
        "secs": time.monotonic() - t,
        "log": out,
    }


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("sweep", nargs="*", help="which sweeps to run")
    ap.add_argument("--all", action="store_true", help="every sweep")
    ap.add_argument("--list", action="store_true")
    ap.add_argument(
        "--jobs", type=int, default=16, help="concurrent Vivado runs (default 16)"
    )
    # MEASURED 2026-08-26 with ten jobs in flight: peak 1.59 GB, mean 1.44. The
    # 15.7 this used to claim was a NON-LEAN run -- `run()` sets
    # KOHAKU_OOC_LEAN, and it is the timing forensics, not the device database,
    # that costs the memory. At 15.7 the guard capped a 122 GB machine to 5 jobs.
    ap.add_argument(
        "--mem-per-job",
        type=float,
        default=2.0,
        help="GB assumed per Vivado run when capping --jobs (measured 1.6 lean)",
    )
    ap.add_argument(
        "--no-mem-cap",
        action="store_true",
        help="run the requested --jobs even if the memory guard would lower it",
    )
    args = ap.parse_args()

    if args.list or not (args.sweep or args.all):
        for name, (core, why, points) in SWEEPS.items():
            print(f"  {name:<14} {core}  {len(points)} points -- {why}")
        n = sum(len(p) for _, _, p in SWEEPS.values())
        print(f"\n  {len(SWEEPS)} sweeps, {n} synthesis runs in total")
        return 0

    names = list(SWEEPS) if args.all else args.sweep
    for n in names:
        if n not in SWEEPS:
            sys.exit(f"unknown sweep {n!r}; --list to see them")

    # A Vivado run holds the xcvu13p device database whatever the design costs,
    # so concurrency is bounded by MEMORY, never by cores. 12 jobs took 111 GB
    # and stalled the machine; this makes that unreachable by accident.
    free = free_gb()
    if free is not None and not args.no_mem_cap:
        cap = max(1, int((free * 0.70) // args.mem_per_job))
        if cap < args.jobs:
            print(
                f"  {free:.0f} GB free / {args.mem_per_job:.0f} GB per job:"
                f" --jobs {args.jobs} -> {cap} (70% of free). --no-mem-cap overrides."
            )
            args.jobs = cap

    # EVERY ROW OF EVERY SWEEP AT ONCE, so the pool stays full: sweeps have
    # different lengths and running them one after another idles the tail.
    # ONE SYNTHESIS PER DISTINCT ARGV: nine rows ARE the base spelled
    # differently, and two runs of one config have disagreed here before.
    jobs, seen, plan = [], {}, {}
    for name in names:
        core, _why, points = SWEEPS[name]
        script, base, pos = CORES[core]
        for i, pt in enumerate(points):
            label = ",".join(f"{k}={v}" for k, v in pt.items()) or "baseline"
            argv = argv_for(pt, base, pos)
            key = (script, tuple(argv))
            if key not in seen:
                seen[key] = f"{name}_{i}"
                jobs.append((seen[key], argv, script))
            plan[(name, i)] = (seen[key], label)

    # BEFORE the runs, not after: build/sweep accumulates across campaigns whose
    # retired knobs sit at positions that now mean something else (177 such rows
    # were there), and a campaign that dies half way must still be attributable.
    WORK.mkdir(parents=True, exist_ok=True)
    # `--all` DEFINES a campaign; a named subset EXTENDS it. Replacing on a
    # subset run threw away the other 78 rows and left the estimator fitting
    # from two.
    tags = {t for t, _a, _s in jobs}
    man = WORK / "manifest.json"
    if not args.all and man.exists():
        tags |= set(json.loads(man.read_text()).get("tags", []))
    man.write_text(json.dumps({"tags": sorted(tags)}, indent=2))

    shared = len(plan) - len(jobs)
    print(
        f"  {len(jobs)} synthesis runs, {args.jobs} at a time"
        f"  ({len(plan)} rows, {shared} sharing an identical build)\n",
        flush=True,
    )
    results = {}
    with ThreadPoolExecutor(max_workers=args.jobs) as ex:
        futs = {ex.submit(run, a, s, t): t for t, a, s in jobs}
        for done, fut in enumerate(as_completed(futs), start=1):
            tag = futs[fut]
            r = fut.result()
            results[tag] = r
            lut = r["lut"] if r["lut"] is not None else "FAILED"
            print(
                f"  [{done}/{len(jobs)}] {tag:<20} LUT {lut!s:>7}"
                f"  ({r['secs']:.0f}s)",
                flush=True,
            )

    for name in names:
        core, why, points = SWEEPS[name]
        _script, base, pos = CORES[core]
        print(f"\n=== {name} ({core}) ===\n{why}\n")
        print(f"  base: {describe(base, pos)}")
        print("  every row is this, with only the named knob changed\n")
        rows = [
            (plan[(name, i)][1], results[plan[(name, i)][0]])
            for i in range(len(points))
        ]
        for lb, r in rows:
            lut = r["lut"] if r["lut"] is not None else "FAILED"
            print(
                f"  {lb:<30} LUT {lut!s:>7}  FF {r['ff']!s:>7}"
                f"  DSP {r['dsp']!s:>4}  BRAM {r['bram']:>6}"
                f"  {r['fmax']:>7} MHz"
            )
            if r["lut"] is None:
                bad = [x for x in r["log"].splitlines() if "ERROR" in x][:3]
                if not bad:
                    bad = ["no ERROR line; last 8 of the log:"] + [
                        x for x in r["log"].splitlines() if x.strip()
                    ][-8:]
                for b in bad:
                    print(f"      {b}")
        good = [(lb, r) for lb, r in rows if r["lut"] is not None]
        for (la, ra), (lb, rb) in itertools.pairwise(good):
            print(
                f"\n  marginal: {la} -> {lb} is "
                f"{rb['lut'] - ra['lut']:+d} LUT, {rb['ff'] - ra['ff']:+d} FF"
            )
        if len(good) > 2:
            (la, ra), (lb, rb) = good[0], good[-1]
            print(
                f"  span:     {la} -> {lb} is "
                f"{rb['lut'] - ra['lut']:+d} LUT, {rb['ff'] - ra['ff']:+d} FF"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
