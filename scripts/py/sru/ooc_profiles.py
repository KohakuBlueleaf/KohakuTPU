"""OOC synthesis of the SRU mesh endpoint (sru_cu) for every profile and
knob variant in profiles.py, at once, every report on disk
(scripts/tcl/ooc_mod.tcl):

    python scripts/py/sru/ooc_profiles.py [--jobs N] [--period 3.333] [names...]

build/ooc/sru_cu_<profile>/ holds result.txt (one line), util.rpt,
hier.rpt (depth 6), census.txt, timing.rpt, timing_summary.rpt, synth.log.
"""

import argparse
import pathlib
import subprocess
import sys
import time

import profiles

ROOT = pathlib.Path(__file__).resolve().parents[3]
VIVADO = r"D:\Xilinx\Vivado\2024.2\bin\vivado.bat"
S = "src/kohakutpu/sru"
FILES = [
    "src/kohakuaccel/common/sync_fifo.v",
    "src/kohakuaccel/common/kh_rst_sync.v",
    "src/kohakuaccel/noc/endpoint/noc_cu_base.v",
    "src/kohakuaccel/common/kohaku_sdpram.v",
    "src/kohakutpu/vector2/v2_cvt.v",
    f"{S}/sru_cfg.vh",
    f"{S}/stream/sru_f16e8.vh",
    f"{S}/lane/sru_dsp.v",
    f"{S}/lane/sru_plane.v",
    f"{S}/pool/sru_pool.v",
    f"{S}/stream/sru_unpack.v",
    f"{S}/stream/sru_red.v",
    f"{S}/stream/sru_sum2e8.v",
    f"{S}/stream/sru_mx7.v",
    f"{S}/stream/sru_e8f16_acc.v",
    f"{S}/sru_core.v",
    f"{S}/sru_cu.v",
]


def generics(p):
    return "+".join(f"{k}:{v}" for k, v in profiles.knobs(p).items())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*")
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--period", default="3.333")
    args = ap.parse_args()
    names = args.names or list(profiles.PROFILES) + list(profiles.VARIANTS)

    todo = list(names)
    running = {}
    while todo or running:
        while todo and len(running) < args.jobs:
            p = todo.pop(0)
            out = ROOT / "build" / "ooc" / f"sru_cu_{p}"
            cmd = [
                VIVADO, "-mode", "batch", "-nojournal", "-nolog",
                "-source", "scripts/tcl/ooc_mod.tcl", "-tclargs",
                out.as_posix(), "sru_cu", args.period, "clk", generics(p), *FILES,
            ]
            log = open(ROOT / "build" / "ooc" / f"sru_cu_{p}.out", "w")
            running[p] = (subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT), log)
            print(f"started {p}", flush=True)
            # Vivados started together race on their init scripts (unimacro
            # "couldn't read file"): one start every 15 s
            time.sleep(15)
        for p in list(running):
            proc, log = running[p]
            if proc.poll() is not None:
                log.close()
                res = ROOT / "build" / "ooc" / f"sru_cu_{p}" / "result.txt"
                line = res.read_text().strip() if res.exists() else f"FAILED exit {proc.returncode}"
                print(f"{p:8s} {line}", flush=True)
                del running[p]
        time.sleep(5)
    sys.exit(0)


if __name__ == "__main__":
    main()
