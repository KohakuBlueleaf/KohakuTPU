"""Measure one KohakuDSP configuration at several clock targets; append the rows.

    python tests/pe/tools/khd_frontier.py --csv <dir>/matrix.csv \
        --work build/ooc/khd --config s8 --targets 5.0,3.333,2.5

A row is (configuration x clock target), not a configuration, because **LUT is
not independent of the constraint**: the base core measured 92 LUT spent
chasing a target it was already meeting, and 232 and 387 more below that. A
resource figure only means something beside the target it was asked for.

A CONFIGURATION IS GATED AS ITSELF. The component test is generated for the
same parameters the synthesis is given -- a build without the permute unit is
verified on a stream that never uses one -- and a configuration that fails
contributes no row at all.

The exception, stated rather than hidden: `VSPAD_ENTRIES` is verified at 64 and
synthesised at 1024. It is a capacity parameter with no behaviour beyond the
address range, and the range check is exercised; the depth is what the BRAM
count measures.

Vivado runs ONE AT A TIME. Two OOC runs on this machine intermittently fail
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
TCL = ROOT / "scripts" / "tcl" / "ooc_khd.tcl"
RUN = ROOT / "tests" / "pe" / "tools" / "khd_run.py"

#: name -> simd, vregs, nacc, vspad, muls, shift, perm, wb, use_dsp, vreg_prim,
#: f16, npart. NPART is ARCHITECTURAL -- float addition does not associate, so
#: two builds with different counts compute different answers on one program.
CONFIGS = {
    # the width sweep: 09 S27's Stage 2 question, at equal everything else
    "s2":        (2, 8, 2, 1024, 4, 1, 1, 0, "yes", "distributed"),
    "s4":        (4, 8, 2, 1024, 4, 1, 1, 0, "yes", "distributed"),
    "s8":        (8, 8, 2, 1024, 4, 1, 1, 0, "yes", "distributed"),
    # with X / without X, all at SIMD 8
    "s8-nosh":   (8, 8, 2, 1024, 4, 0, 1, 0, "yes", "distributed"),
    "s8-nopm":   (8, 8, 2, 1024, 4, 1, 0, 0, "yes", "distributed"),
    "s8-m2":     (8, 8, 2, 1024, 2, 1, 1, 0, "yes", "distributed"),
    "s8-lutmul": (8, 8, 2, 1024, 4, 1, 1, 0, "no",  "distributed"),
    "s8-v32":    (8, 32, 2, 1024, 4, 1, 1, 0, "yes", "distributed"),
    "s8-v4":     (8, 4, 2, 1024, 4, 1, 1, 0, "yes", "distributed"),
    "s8-a1":     (8, 8, 1, 1024, 4, 1, 1, 0, "yes", "distributed"),
    "s8-a4":     (8, 8, 4, 1024, 4, 1, 1, 0, "yes", "distributed"),
    "s8-bramrf": (8, 8, 2, 1024, 4, 1, 1, 0, "yes", "block"),
    # the write a stage later: a second stall for a shorter path
    "s8-wb":     (8, 8, 2, 1024, 4, 1, 1, 1, "yes", "distributed"),
    "s2-wb":     (2, 8, 2, 1024, 4, 1, 1, 1, "yes", "distributed"),
    "s8-wb-nopm": (8, 8, 2, 1024, 4, 1, 0, 1, "yes", "distributed"),
    # everything the matrix can turn off, turned off
    "s8-min":    (8, 4, 1, 1024, 2, 0, 0, 0, "yes", "distributed"),
}
for _n, _c in list(CONFIGS.items()):
    CONFIGS[_n] = _c + (0, 0)

#: The float tier. `s8-f16only` answers whether a float-only DSP PE is a thing;
#: `s8-f16-a4` prices the rotation contract in accumulators.
CONFIGS.update({
    "s8-f16":     (8, 8, 2, 1024, 4, 1, 1, 0, "yes", "distributed", 1, 16),
    "s8-f16-a4":  (8, 8, 4, 1024, 4, 1, 1, 0, "yes", "distributed", 1, 16),
    "s8-f16only": (8, 8, 2, 1024, 2, 0, 0, 0, "yes", "distributed", 1, 16),
    "s4-f16":     (4, 8, 2, 1024, 4, 1, 1, 0, "yes", "distributed", 1, 16),
})

#: Declared RTL revisions. A row carries the one it was measured on, and a name
#: not declared here is a typo until someone declares it -- because a
#: configuration row from before an RTL change is not comparable to one from
#: after it, and an unlabelled row is not a measurement.
REVISIONS = {
    "r1": "first datapath: decode in MEM, HAS_SHIFT refused the encoding only",
    "r2": "decode registered at EX->MEM; HAS_SHIFT removes khd_pshift32",
    "r3": "WB_STAGE parameter: the vector file may be written a cycle later",
    "r4": "SWAR adder (one native carry chain); the rounding shift gets its own",
    "r5": "vdot at II=1 back to back; the scalar store moves off the NoC's "
          "scratchpad port onto the vector unit's, and its interlock becomes a "
          "decode bubble",
    "r6": "cmp_sub decided in EX and registered, off the head of the binding path",
    "r7": "the vector file's stall holds its OUTPUT register, not its read "
          "address, so the MEM stall is off the array's address path",
    "r8": "the float tier: rotating partials in two mirrored distributed RAMs "
          "rather than an indexed flop array, a one-shot zero/seed sweep, and "
          "a drain hazard so an accumulator read waits for the lane",
    "r9": "the float tier's FP16 conversions are ONE converter walked over the "
          "slots rather than one per slot: vfaccrd and vfaccwr already hold the "
          "MEM stage for hundreds of cycles, and 32 parallel converters were "
          "3,296 LUT of hardware that ran once per kernel",
}

FIELDS = [
    "rtl", "config", "target_ns", "fmax_mhz", "slack_ns", "levels",
    "lut", "lut_logic", "lutram", "ff", "bram", "dsp", "ctrlsets",
    "simd", "vregs", "nacc", "vspad", "muls", "shift", "perm", "wb",
    "f16", "npart",
    "use_dsp", "vreg_prim", "gates", "binding_path",
]

REC = re.compile(r"^@@@REC .*?\blut=([\d.]+).*?\blut_log=([\d.]+)"
                 r".*?\blut_dram=([\d.]+).*?\bff=([\d.]+).*?\bbram=([\d.]+)"
                 r".*?\bdsp=([\d.]+).*?\bctrlsets=(\d+)")
CLK = re.compile(r"^@@@ noc_clk\s+([\d.]+) MHz\s+slack ([-+\d.]+)\s+req [\d.]+"
                 r"\s+lvl (\S+)\s+(.*)$")


def take_lock(work_root):
    work_root.mkdir(parents=True, exist_ok=True)
    lock = work_root / ".sweep-lock"
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        print("  ANOTHER SWEEP HOLDS %s (pid %s). Wait for it, or delete the "
              "lock if it is stale." % (lock, lock.read_text().strip()))
        return None
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return lock


def run_ooc(cfg, target, work):
    (simd, vregs, nacc, vspad, muls, sh, pm, wb, udsp, vprm,
     f16, npart) = CONFIGS[cfg]
    work.mkdir(parents=True, exist_ok=True)
    args = [simd, vregs, nacc, vspad, muls, sh, pm, udsp, vprm, target, wb,
            f16, npart]
    cmd = [str(VIVADO), "-mode", "batch", "-notrace", "-source", str(TCL),
           "-tclargs"] + [str(x) for x in args]
    log = work / "vivado.log"
    for attempt in range(3):
        subprocess.run(cmd, cwd=str(work), stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)
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
            (out["lut"], out["lut_logic"], out["lutram"], out["ff"],
             out["bram"], out["dsp"], out["ctrlsets"]) = m.groups()
        m = CLK.match(ln)
        if m:
            out["fmax_mhz"], out["slack_ns"] = m.group(1), m.group(2)
            out["levels"], out["binding_path"] = m.group(3), m.group(4).strip()
    return out or None


def run_gate(cfg, wall):
    (simd, vregs, nacc, _vspad, muls, sh, pm, wb, _udsp, vprm,
     f16, _npart) = CONFIGS[cfg]
    cmd = [sys.executable, str(RUN), "--simd", str(simd), "--muls", str(muls),
           "--vregs", str(vregs), "--nacc", str(nacc), "--wall", str(wall),
           "--vreg-prim", vprm, "--wb-stage", str(wb)]
    if not sh:
        cmd.append("--no-shift")
    if not pm:
        cmd.append("--no-perm")
    if f16:
        cmd.append("--f16")
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                       check=False)
    return "PASS" if r.returncode == 0 else "FAIL", r.stdout


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--csv", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--config", required=True, choices=sorted(CONFIGS))
    ap.add_argument("--targets", default="5.0,3.333,2.5")
    ap.add_argument("--wall", type=float, default=600.0)
    ap.add_argument("--no-gates", action="store_true")
    ap.add_argument("--rtl", required=True, choices=sorted(REVISIONS),
                    help="the RTL revision these rows were measured on")
    a = ap.parse_args()

    csv_path = pathlib.Path(a.csv)
    work_root = pathlib.Path(a.work)
    lock = take_lock(work_root)
    if lock is None:
        return 1
    atexit.register(lambda: lock.unlink(missing_ok=True))

    (simd, vregs, nacc, vspad, muls, sh, pm, wb, udsp, vprm,
     f16, npart) = CONFIGS[a.config]

    if a.no_gates:
        gates = "not run"
    else:
        print("  gate for %s ..." % a.config, flush=True)
        gates, text = run_gate(a.config, a.wall)
        print("  gate: %s" % gates, flush=True)
        if gates != "PASS":
            print(text[-3000:])
            print("  NOT A POINT: %s fails its own component test" % a.config)
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
            row = {"rtl": a.rtl, "config": a.config, "target_ns": t,
                   "gates": gates,
                   "simd": simd, "vregs": vregs, "nacc": nacc, "vspad": vspad,
                   "muls": muls, "shift": sh, "perm": pm, "wb": wb,
                   "f16": f16, "npart": npart,
                   "use_dsp": udsp, "vreg_prim": vprm}
            row.update(got)
            w.writerow(row)
            fh.flush()
            print("    %s MHz  lut %s  ff %s  dsp %s  bram %s  slack %s"
                  % (got.get("fmax_mhz"), got.get("lut"), got.get("ff"),
                     got.get("dsp"), got.get("bram"), got.get("slack_ns")),
                  flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
