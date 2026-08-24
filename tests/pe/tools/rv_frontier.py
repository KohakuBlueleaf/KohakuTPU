"""Measure one PE configuration at several clock targets and append the rows.

    python tests/pe/tools/rv_frontier.py --csv <file> --config balanced \
        --targets 5.0,3.333,2.5,2.2,2.0

A frontier POINT is a design variant crossed with an OOC clock target, because
**LUT is not independent of the constraint**: synthesis spends area chasing
timing, so a resource figure only means something beside the target it was asked
for.  One variant therefore contributes a curve, not a dot.

Cycle counts ride along on every row.  A low-LUT point that costs 2x the cycles
is a different animal from one at the same cycles, and a frontier that omits
them is not honest.  They come from the benches, which depend on the RTL and not
on the clock target, so one bench run serves every target of a variant.

The CSV path and the working directory are REQUIRED arguments with no defaults:
this file is tracked, and where the research lives is not its business.

Vivado runs ONE AT A TIME.  Two OOC runs on this machine intermittently fail
reading the install tree, which is environmental and retried here.
"""

import argparse
import atexit
import csv
import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
VIVADO = pathlib.Path(r"D:\Xilinx\Vivado\2024.2\bin\vivado.bat")
TCL = ROOT / "scripts" / "tcl" / "ooc_rv_pe.tcl"
RUN = ROOT / "tests" / "pe" / "tools" / "rv_run.py"

FIELDS = [
    "config",
    "arm",
    "target_ns",
    "fmax_mhz",
    "slack_ns",
    "levels",
    "lut",
    "lut_logic",
    "lutram",
    "ff",
    "bram",
    "ctrlsets",
    "mc1_c0",
    "mc4_c0",
    "mc4_c1",
    "mc4_c2",
    "mc4_c3",
    "rt_cycles",
    "gates",
    "binding_path",
    "params",
]

#: name -> the seven RTL knobs ooc_rv_pe.tcl takes, and which arm it belongs to.
#: Every point is a CONFIGURATION of one design; nothing here forks a file.
CONFIGS = {
    # regfile, l1_lines, fwd_x, btb, imem, spad
    "balanced": ("-", "distributed", 128, 1, 32, 2048, 2048),
    "bal-bram": ("-", "block", 128, 1, 32, 2048, 2048),
    # Arm A -- strip for area, spend cycles.
    "a-nofwd": ("A", "distributed", 128, 0, 32, 2048, 2048),
    "a-btb0": ("A", "distributed", 128, 1, 0, 2048, 2048),
    "a-nofwd0": ("A", "distributed", 128, 0, 0, 2048, 2048),
    "a-l164": ("A", "distributed", 64, 1, 32, 2048, 2048),
    "a-min": ("A", "distributed", 64, 0, 0, 2048, 2048),
    # a-min without the one knob measured to be a pure loss: FWD_X=0 buys 2 LUT
    # and costs 5.4 MHz, so a-min's low corner is carrying dead weight.
    "a-l164b0": ("A", "distributed", 64, 1, 0, 2048, 2048),
    # Arm B -- spend area for frequency.
    "b-bram-rf": ("B", "block", 128, 1, 32, 2048, 2048),
    "b-btb64": ("B", "distributed", 128, 1, 64, 2048, 2048),
}

REC = re.compile(
    r"^@@@REC .*?\blut=(\d+).*?\blut_log=(\d+).*?\blut_dram=(\d+)"
    r".*?\bff=(\d+).*?\bbram=([\d.]+).*?\bctrlsets=(\d+)"
)
CLK = re.compile(
    r"^@@@ noc_clk\s+([\d.]+) MHz\s+slack ([-+\d.]+)\s+req [\d.]+"
    r"\s+lvl (\S+)\s+(.*)$"
)


def take_lock(work_root):
    """Two sweeps sharing a work directory run two Vivados into one log and the
    rows that come back are nobody's measurement. Refuse rather than race."""
    work_root.mkdir(parents=True, exist_ok=True)
    lock = work_root / ".sweep-lock"
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        print(
            "  ANOTHER SWEEP HOLDS %s (pid %s). Wait for it, or delete the "
            "lock if it is stale." % (lock, lock.read_text().strip())
        )
        return None
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return lock


def run_ooc(cfg, target, work):
    """One synthesis. Returns the parsed row fields, or None if it failed."""
    _, rfp, lin, fwx, btb, imw, spw = CONFIGS[cfg]
    work.mkdir(parents=True, exist_ok=True)
    args = ["rv_pe", rfp, lin, fwx, btb, imw, spw, "default", target]
    cmd = [
        str(VIVADO),
        "-mode",
        "batch",
        "-notrace",
        "-source",
        str(TCL),
        "-tclargs",
    ] + [str(a) for a in args]
    log = work / "vivado.log"
    for attempt in range(3):
        subprocess.run(
            cmd,
            cwd=str(work),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if not log.exists():
            return None
        text = log.read_text(errors="ignore")
        if "@@@REC " in text:
            break
        if "couldn't read file" in text:
            print("    install-tree read failure, retry %d/2" % (attempt + 1))
            continue
        return None
    else:
        return None

    out = {}
    for ln in text.splitlines():
        m = REC.match(ln)
        if m:
            out["lut"], out["lut_logic"], out["lutram"] = m.group(1, 2, 3)
            out["ff"], out["bram"], out["ctrlsets"] = m.group(4, 5, 6)
        m = CLK.match(ln)
        if m:
            out["fmax_mhz"], out["slack_ns"] = m.group(1), m.group(2)
            out["levels"], out["binding_path"] = m.group(3), m.group(4).strip()
    return out or None


CYC = re.compile(r"core(\d) \(\d,\d\)\s+halt \w+\s+code \w+\s+cycles\s+(\d+)")
RT = re.compile(r"round trip\s+(\d+) cycles")
ISO = re.compile(r"--- independent programs: (\d) PEs.*?(?=\n--- |\Z)", re.DOTALL)


def sim_defines(cfg):
    """The same knobs the synthesis gets, in the benches' spelling. Without
    this the benches measure the default design and the row reports cycle
    counts belonging to something nobody built."""
    _, rfp, lin, fwx, btb, _, _ = CONFIGS[cfg]
    d = [
        "-d",
        "RV_L1_LINES=%d" % lin,
        "-d",
        "RV_BTB=%d" % btb,
        "-d",
        "RV_FWD_X=%d" % fwx,
    ]
    if rfp == "block":
        d += ["-d", "RV_RF_BRAM"]
    return d


def run_gates(cfg, wall):
    """Every bench, and the cycle counts the frontier carries. A point that
    fails a gate is not a point, so this returns the verdict too."""
    cyc = {
        "mc1_c0": "",
        "mc4_c0": "",
        "mc4_c1": "",
        "mc4_c2": "",
        "mc4_c3": "",
        "rt_cycles": "",
    }
    verdicts = []
    for gate in (1, 2, 3, 4):
        r = subprocess.run(
            [sys.executable, str(RUN), "--gate", str(gate), "--wall", str(wall)]
            + sim_defines(cfg),
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        text = r.stdout
        verdicts.append(
            "PASS" if ("PASS --" in text and "FAIL" not in text) else "FAIL"
        )
        if gate != 4:
            continue
        # KEYED ON THE PE COUNT IN THE HEADER, never on block position: the
        # three benches' output does not always arrive in one piece, and
        # counting blocks silently read mc2's core 0 as mc1's. Each block runs
        # to the next case header, or the later cases overwrite the isolation
        # numbers this is here to collect.
        for m in ISO.finditer(text):
            cores = CYC.findall(m.group(0))
            if m.group(1) == "1" and cores:
                cyc["mc1_c0"] = cores[0][1]
            elif m.group(1) == "4":
                for who, val in cores:
                    cyc["mc4_c%s" % who] = val
        rt = RT.findall(text)
        if rt:
            cyc["rt_cycles"] = rt[0]
    return cyc, ("PASS" if all(v == "PASS" for v in verdicts) else "/".join(verdicts))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--csv", required=True, help="where the points are appended")
    ap.add_argument("--work", required=True, help="directory for the OOC runs")
    ap.add_argument("--config", required=True, choices=sorted(CONFIGS))
    ap.add_argument("--targets", default="5.0,3.333,2.5,2.2,2.0")
    ap.add_argument("--wall", type=float, default=400.0)
    ap.add_argument(
        "--no-gates",
        action="store_true",
        help="synthesis only; the cycle columns stay empty",
    )
    a = ap.parse_args()

    csv_path = pathlib.Path(a.csv)
    work_root = pathlib.Path(a.work)
    lock = take_lock(work_root)
    if lock is None:
        return 1
    # Every path out of here releases it, including the several early returns
    # below and an exception out of Vivado.
    atexit.register(lambda: lock.unlink(missing_ok=True))
    arm, rfp, lin, fwx, btb, imw, spw = CONFIGS[a.config]
    params = "rf=%s l1=%s fwd_x=%s btb=%s imem=%s spad=%s" % (
        rfp,
        lin,
        fwx,
        btb,
        imw,
        spw,
    )

    if a.no_gates:
        cyc, gates = ({k: "" for k in FIELDS if k.startswith(("mc", "rt"))}, "not run")
    else:
        print("  gates for %s ..." % a.config, flush=True)
        cyc, gates = run_gates(a.config, a.wall)
        print("  gates: %s  cycles %s" % (gates, cyc), flush=True)
        if gates != "PASS":
            print("  NOT A POINT: %s fails a gate; nothing appended" % a.config)
            return 1
        # A point without its cycle counts is half a point, and an empty column
        # is indistinguishable from a zero once it is in the CSV.
        missing = [k for k, v in cyc.items() if not v]
        if missing:
            print(
                "  NOT A POINT: gates passed but %s did not parse; the bench "
                "output shape changed" % ", ".join(sorted(missing))
            )
            return 1

    new = not csv_path.exists()
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            w.writeheader()
        for t in [s.strip() for s in a.targets.split(",") if s.strip()]:
            print("  %s @ %s ns ..." % (a.config, t), flush=True)
            got = run_ooc(a.config, t, work_root / ("%s_t%s" % (a.config, t)))
            if got is None:
                print("    FAILED, no row")
                continue
            row = {
                "config": a.config,
                "arm": arm,
                "target_ns": t,
                "gates": gates,
                "params": params,
            }
            row.update(cyc)
            row.update(got)
            w.writerow(row)
            fh.flush()
            print(
                "    %s MHz  lut %s  ff %s  slack %s"
                % (
                    got.get("fmax_mhz"),
                    got.get("lut"),
                    got.get("ff"),
                    got.get("slack_ns"),
                ),
                flush=True,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
