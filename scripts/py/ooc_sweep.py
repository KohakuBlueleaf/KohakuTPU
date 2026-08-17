#!/usr/bin/env python3
"""Run OOC synthesis configurations in parallel and collect every metric.

    python scripts/py/ooc_sweep.py station fw512
    python scripts/py/ooc_sweep.py line s4
    python scripts/py/ooc_sweep.py smc base

Each configuration gets its own working directory and its own Vivado. The Tcl
emits `@@@REC` / `@@@FMAX` / `@@@HIER` lines; this collects them into
`build/sweep_<name>.md` so a table never needs a second synthesis run.

JOBS is 8: at under ~20k LUT per job that fits the machine alongside one other
Vivado. Raising it past that starts losing runs to install-tree read failures.
"""

import argparse
import pathlib
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from jobguard import JobGuard, avail_gb, run_guarded  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
VIVADO = pathlib.Path(r"D:\Xilinx\Vivado\2024.2\bin\vivado.bat")
JOBS = 8
MIN_FREE_GB = 24.0

GUARD = None

# A line job costs ~10 GiB RSS, NOT the ~3 GiB "peak" its synthesis report
# prints; six crossed the 24 GiB floor from 80.
SUITE_JOBS = {"smc-base": 3, "xbar-anchor": 3, "line-preset": 3,
              "line-width": 3, "line-freq": 3, "line-ports": 3}

#: name -> (tcl script, [tclargs...])
STATION = "scripts/tcl/ooc_station.tcl"
LINE = "scripts/tcl/ooc_line_sweep.tcl"
SMC = "scripts/tcl/ooc_smc_pseudo.tcl"

# ooc_station.tcl: top dir preset lpb tagw ost sfwd fw tmo aw
def station(tag, *, fw=512, aw=43, ost=4, sfwd=1, lpb=820, tmo=0, preset=0):
    tagw = 1 if ost <= 1 else 4
    return (tag, STATION,
            ["sb_root9", "default", preset, lpb, tagw, ost, sfwd, fw, tmo, aw])

# ooc_line_sweep.tcl: fw nq cdc period tag lpb ost sfwd half aw
def line(tag, *, fw=512, nq=4, cdc=1, per="5.000", lpb=820, ost=4, sfwd=1,
         half=0, aw=43, nm=3):
    return (tag, LINE, [fw, nq, cdc, per, tag, lpb, ost, sfwd, half, aw, nm])

# ooc_smc_pseudo.tcl: nsi nmi si_dw mi_dw nclk tag
def smc(tag, *, nsi=3, nmi=9, sidw=512, midw=512, nclk=4,
        ip="smartconnect", strat=2):
    return (tag, SMC, [nsi, nmi, sidw, midw, nclk, tag, ip, strat])


SUITES = {
    # Every preset at the drop-in-valid width, with and without block RAM.
    "station-fw512": [
        station("st-minarea-nobram", ost=1, sfwd=0, lpb=820),
        station("st-minarea-bram",   ost=1, sfwd=0, lpb=0),
        station("st-balanced-nobram", ost=4, sfwd=1, lpb=820),
        station("st-balanced-bram",   ost=4, sfwd=1, lpb=0),
        station("st-perf-nobram",     ost=8, sfwd=1, lpb=820),
        station("st-perf-bram",       ost=8, sfwd=1, lpb=0),
        station("st-safe-nobram",     ost=4, sfwd=1, lpb=820, tmo=4000),
        station("st-balanced-fw256",  ost=4, sfwd=1, lpb=820, fw=256),
    ],
    # Flit and address width, holding everything else fixed.
    "station-width": [
        station("w-fw128-aw43", fw=128), station("w-fw256-aw43", fw=256),
        station("w-fw512-aw43", fw=512), station("w-fw1024-aw43", fw=1024),
        station("w-fw512-aw32", fw=512, aw=32),
        station("w-fw512-aw40", fw=512, aw=40),
        station("w-fw512-aw48", fw=512, aw=48),
        station("w-fw512-aw64", fw=512, aw=64),
    ],
    # Port count in the deployed topology. sb_stn_root, which build_ports()
    # wraps, fixes DSTW=3 and SRCW=2 and cannot express Q>8 or K>4 at all.
    "line-ports": [
        line("q-1", nq=1), line("q-2", nq=2), line("q-4", nq=4),
        line("q-6", nq=6), line("q-8", nq=8),
        line("m-1", nm=1), line("m-2", nm=2), line("m-6", nm=6),
    ],
    # Both built by build_ports(); the wrappers do not exist until then.
    "station-ports": [],
    # The KohakuAccel per-die shapes at the width it deploys: 3 masters and 4
    # slaves on SLR1, 1 and 4 elsewhere. Replaces the derived per-SLR figures.
    "deploy-fw256": [],
    # No ln-safe: TIMEOUT is not an argument of ooc_line_sweep.tcl, so it would
    # be ln-balanced under a second name.
    "line-preset": [
        line("ln-minarea", ost=1, sfwd=0), line("ln-balanced", ost=4, sfwd=1),
        line("ln-perf", ost=8, sfwd=1), line("ln-bram", lpb=0),
        line("ln-fw256", fw=256), line("ln-fw256-bram", fw=256, lpb=0),
        line("ln-full", half=1), line("ln-nocdc", cdc=0),
    ],
    # Flit width at line level, and the wide-slave-behind-narrow-fabric case.
    "line-width": [
        line("lw-fw128", fw=128), line("lw-fw256", fw=256),
        line("lw-fw512", fw=512), line("lw-fw1024", fw=1024),
        line("lw-fw256-aw32", fw=256, aw=32),
        line("lw-fw256-aw64", fw=256, aw=64),
        line("lw-fw512-aw32", fw=512, aw=32),
        line("lw-fw512-aw64", fw=512, aw=64),
    ],
    "line-freq": [
        line("f-150", per="6.667"), line("f-200", per="5.000"),
        line("f-250", per="4.000"), line("f-300", per="3.333"),
        line("f-350", per="2.857"), line("f-400", per="2.500"),
        line("f-450", per="2.222"), line("f-500", per="2.000"),
    ],
    "smc-base": [
        smc("smc-3x9-4clk"), smc("smc-3x9-1clk", nclk=1),
        smc("smc-3x5-4clk", nmi=5), smc("smc-1x5-2clk", nsi=1, nmi=5, nclk=2),
        smc("smc-2x6-3clk", nsi=2, nmi=6, nclk=3),
        smc("smc-3x9-256", sidw=256, midw=256),
        smc("smc-6x9-4clk", nsi=6), smc("smc-3x16-4clk", nmi=16),
    ],
    # axi_interconnect anchor points, both strategies.
    "xbar-anchor": [
        smc("xbar-3x9-perf", ip="axi_interconnect", strat=2),
        smc("xbar-3x9-area", ip="axi_interconnect", strat=1),
        smc("xbar-3x9-1clk-perf", ip="axi_interconnect", strat=2, nclk=1),
        smc("xbar-3x9-1clk-area", ip="axi_interconnect", strat=1, nclk=1),
        smc("xbar-3x5-perf", ip="axi_interconnect", strat=2, nmi=5),
        smc("xbar-3x5-area", ip="axi_interconnect", strat=1, nmi=5),
        smc("xbar-3x9-256-perf", ip="axi_interconnect", strat=2,
            sidw=256, midw=256),
        smc("xbar-3x9-256-area", ip="axi_interconnect", strat=1,
            sidw=256, midw=256),
    ],
}


# Two families crossing at 3x4, so one fit gives both the per-master and the
# per-slave slope. Uniform 512-bit ports: a mixed-width shape measures the mix.
PORT_SHAPES = [(3, 1), (3, 2), (3, 4), (3, 8)]


def build_ports(shapes=None, fw=512, aw=43):
    """Generate a station wrapper per shape, return its jobs.

    shapes defaults to PORT_SHAPES. The module name carries fw so the same
    shape can be measured at two flit widths without clobbering its wrapper.
    """
    gen = ROOT / "scripts" / "py" / "gen_station_wrap.py"
    jobs = []
    for k, q in (shapes or PORT_SHAPES):
        # gen_station_wrap hardcodes DSTW=3 / SRCW=2 for the root template, and
        # exceeding either emits RTL whose part-selects are out of range.
        if q > 8 or k > 4:
            raise ValueError(f"sb_stn_root cannot express {k}x{q}: "
                             "DSTW=3 caps Q at 8 and SRCW=2 caps K at 4. "
                             "Use the line-ports suite instead.")
        mod = f"sb_p{k}x{q}f{fw}"
        subprocess.run(
            [sys.executable, str(gen), "--kind", "root", "--nk", str(k),
             "--mgr-w", ",".join([str(fw)] * k),
             "--loc-w", ",".join([str(fw)] * q),
             "--fw", str(fw), "--aw", str(aw),
             "-o", str(ROOT / "src" / "synth_top" / f"{mod}.v"), "-m", mod],
            check=True)
        jobs.append((f"p-{k}x{q}", STATION,
                     [mod, "default", 0, 820, 4, 4, 1, fw, 0, aw]))
    return jobs


def run_one(job):
    tag, tcl, args = job
    wd = ROOT / "build" / "sweep" / tag
    wd.mkdir(parents=True, exist_ok=True)
    cmd = [str(VIVADO), "-mode", "batch", "-notrace",
           "-log", "run.log", "-journal", "run.jou",
           "-source", str(ROOT / tcl), "-tclargs"] + [str(a) for a in args]
    log = wd / "run.log"
    # Concurrent Vivados intermittently fail reading their own install tree
    # ("couldn't read file .../unimacro_verilog.tcl"); it does not repeat.
    for attempt in range(3):
        if GUARD is not None and GUARD.tripped:
            return tag, None
        try:
            run_guarded(GUARD, cmd, cwd=str(wd),
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            print(f"  {tag}: {exc}")
            return tag, None
        if not log.exists():
            return tag, None
        text = log.read_text(errors="ignore")
        if "@@@REC " in text or "couldn't read file" not in text:
            return tag, text
        print(f"  {tag}: install-tree read failure, retry {attempt + 1}/2")
    return tag, text


def collect(text):
    rec, fmax = {}, []
    for ln in text.splitlines():
        if ln.startswith("@@@REC "):
            for kv in ln[len("@@@REC "):].split():
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    rec[k] = v
        elif ln.startswith("@@@FMAX "):
            f = ln.split()
            if len(f) >= 4 and f[3] != "none":
                fmax.append((f[2], f[3]))
    return rec, fmax


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("suite", choices=sorted(SUITES))
    ap.add_argument("--jobs", type=int, default=None)
    args = ap.parse_args()
    if args.jobs is None:
        args.jobs = SUITE_JOBS.get(args.suite, JOBS)

    global GUARD
    GUARD = JobGuard(min_free_gb=MIN_FREE_GB)

    if args.suite == "station-ports":
        jobs = build_ports()
    elif args.suite == "deploy-fw256":
        jobs = build_ports([(3, 4), (1, 4)], fw=256)
    else:
        jobs = SUITES[args.suite]
    print(f"{args.suite}: {len(jobs)} configs, {args.jobs} at a time, "
          f"{avail_gb():.0f} GiB free, floor {MIN_FREE_GB:.0f}")

    rows = []
    with ThreadPoolExecutor(max_workers=args.jobs) as ex:
        for tag, text in ex.map(run_one, jobs):
            if text is None:
                print(f"  {tag}: FAILED")
                rows.append((tag, {}, []))
                continue
            rec, fmax = collect(text)
            if not rec:
                print(f"  {tag}: no @@@REC (synth failed)")
            else:
                print(f"  {tag}: lut={rec.get('lut')} ff={rec.get('ff')} "
                      f"bram={rec.get('bram')}")
            rows.append((tag, rec, fmax))

    out = ROOT / "build" / f"sweep_{args.suite}.md"
    keys = ["lut", "lut_log", "lut_mem", "lut_dram", "lut_srl", "ff", "bram",
            "ctrlsets"]
    lines = [f"# {args.suite}", "",
             "| config | " + " | ".join(keys) + " |",
             "|" + "---|" * (len(keys) + 1)]
    for tag, rec, _ in rows:
        lines.append("| " + tag + " | " +
                     " | ".join(rec.get(k, "-") for k in keys) + " |")
    lines += ["", "## Fmax", "", "| config | clock | MHz |", "|---|---|---|"]
    for tag, _, fmax in rows:
        for clk, mhz in fmax:
            lines.append(f"| {tag} | {clk} | {mhz} |")
    lines += ["", "## Configuration", "", "| config | settings |", "|---|---|"]
    for tag, rec, _ in rows:
        cfg = " ".join(f"{k}={v}" for k, v in rec.items() if k not in keys)
        lines.append(f"| {tag} | {cfg} |")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    if GUARD.tripped:
        print("jobguard tripped: results above are incomplete")
    GUARD.close()


if __name__ == "__main__":
    main()
