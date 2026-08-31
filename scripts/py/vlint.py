#!/usr/bin/env python3
"""Lint the RTL with verilator, using a bench's own source list.

    python scripts/py/vlint.py sb_mesh2_ctrl
    python scripts/py/vlint.py --list
    python scripts/py/vlint.py sb_mesh2_ctrl --top ktpu_ship_1x1_2c2v_1m

The list comes from `xsim.py` for the same reason `ooc_mesh.py` takes it from
there: a second hand-kept list drifts, and the first symptom is a lint run
against RTL the benches no longer use.

Testbench files are excluded -- they use simulation constructs verilator lints
differently, and the point here is the synthesisable tree.
"""

import argparse
import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "py"))

import xsim

MAMBA_ENV = pathlib.Path(os.environ["USERPROFILE"]) / "micromamba/envs/hdlfmt/Library"

INCDIRS = (
    "src/kohakuaccel/noc",
    "src/kohakuaccel/pe/rv64-sys/core",
    "src/kohakumpe/simd",
    "src/kohakumpe/simd/generated",
    "src/kohakumpe/simt",
    "src/kohakumpe/simt/generated",
)

# DSP48E2 and the xpm_* macros are Vivado's, so without these every module that
# names a primitive is MODMISSING and the run stops before reporting anything
# about our own RTL. `-y` is a library search path: verilator only reads the
# file whose name matches a module it actually needs.
VIVADO = pathlib.Path(r"D:\Xilinx\Vivado\2024.2")
VENDOR_Y = (
    VIVADO / "data/verilog/src/unisims",
    VIVADO / "data/ip/xpm/xpm_memory/hdl",
    VIVADO / "data/ip/xpm/xpm_fifo/hdl",
    VIVADO / "data/ip/xpm/xpm_cdc/hdl",
)

# Waived with the reason, never blanket-disabled. Anything not here is a finding.
WAIVE = (
    # A framework module is parameterised for cases a given top does not use, so
    # an unused parameter or a tied-low port is the normal state of a generate
    # that is off. UNUSEDSIGNAL/UNUSEDPARAM on this tree is thousands of lines of
    # exactly that and hides the findings that matter.
    "UNUSEDSIGNAL",
    "UNUSEDPARAM",
    "UNUSEDGENVAR",
    # Verilog-2001 has no `logic`; every net here is a reg or wire by necessity.
    "DECLFILENAME",
    # A generated top names its instances r1_1, u_cu0 and so on, which is the
    # convention the generator documents.
    "VARHIDDEN",
    # Width truncation at a deliberate slice is idiomatic in this tree and the
    # cases that matter are caught by SELRANGE and WIDTHTRUNC below staying on.
    "WIDTHEXPAND",
    # Vendor primitives carry no `timescale and ours do, which is 107 warnings
    # about Vivado's files and nothing about this design.
    "TIMESCALEMOD",
)

# DSP48E2 reads `glbl.GSR`, a hierarchical reference to an INSTANCE that only
# exists once a simulation top instantiates it. Lint has no such top, so the
# reference is unresolvable and fatal -- and it is in a vendor file. Continue
# past it and let the ours/vendor split below decide what is worth printing.
EXTRA = ("-Wno-fatal",)


def sources_for(bench, top, with_tb=False):
    """The bench's own list. Testbenches are dropped unless `with_tb`.

    They are dropped by default because simulation constructs lint differently
    and the point is the synthesisable tree. But the class that has actually
    cost this tree time lives in the BENCHES: `khs_facc_tb` and `khs_ffold_tb`
    each left the float unit's `op` input unconnected, which is `z`, and every
    result came back X -- reading as a dead accumulator rather than a missing
    pin. `--tb` is how that gets caught next time.
    """
    files = xsim.BENCHES[bench][1]
    keep = [
        f
        for f in dict.fromkeys(files)
        if with_tb or (not f.startswith("tests/") and "/verif/" not in f)
    ]
    if top:
        keep = [f for f in keep if "top/generated/" not in f]
        keep.append(f"src/kohakutpu/top/generated/{top}.v")
    return keep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bench", nargs="?")
    ap.add_argument("--top", help="substitute this generated top")
    ap.add_argument("--list", action="store_true")
    ap.add_argument(
        "--tb",
        action="store_true",
        help="lint the testbenches too, and the top IS the tb",
    )
    args = ap.parse_args()

    if args.list or not args.bench:
        print(" ".join(sorted(xsim.BENCHES)))
        return 0
    if args.bench not in xsim.BENCHES:
        sys.exit(f"vlint: no bench {args.bench}")

    top = args.top or xsim.BENCHES[args.bench][0]
    if not args.tb:
        top = top.removesuffix("_tb")
    env = dict(os.environ, VERILATOR_ROOT=str(MAMBA_ENV))
    cmd = [str(MAMBA_ENV / "bin/verilator_bin.exe"), "--lint-only", "-Wall"]
    cmd += list(EXTRA)
    cmd += [f"-Wno-{w}" for w in WAIVE]
    cmd += [f"-I{(ROOT / d)}" for d in INCDIRS]
    for d in VENDOR_Y:
        if d.is_dir():
            cmd += ["-y", str(d)]
    cmd += ["--top-module", top]
    cmd += [str(ROOT / p) for p in sources_for(args.bench, args.top, args.tb)]
    # DSP48E2 references `glbl` for its global reset, so without this the run
    # ends on one error from a vendor file and reports nothing about our RTL.
    glbl = VIVADO / "data/verilog/src/glbl.v"
    if glbl.is_file():
        cmd.append(str(glbl))

    # check=False deliberately: verilator exits non-zero on a vendor file it
    # cannot elaborate, and this script's exit code is decided by OUR findings
    # below -- raising here would make the gate permanently red.
    p = subprocess.run(
        cmd, cwd=ROOT, env=env, capture_output=True, text=True, check=False
    )
    out = (p.stderr or "") + (p.stdout or "")

    ours, vendor = {}, {}
    # With --tb the benches are ours too, or every finding in the file being
    # linted lands under "vendor" and the run reports itself clean.
    mine = (str(ROOT / "src"),) + ((str(ROOT / "tests"),) if args.tb else ())
    show = []
    # Whether the finding being READ is one of ours, which is not the same as
    # "anything has been printed yet". Testing `show` instead appended every
    # VENDOR finding's continuation lines to the report, because `show` stays
    # non-empty once one of ours has landed in it.
    keep = False
    # Classify on the LOCATION -- the path right after the kind -- not on
    # whether our tree is mentioned anywhere in the message. A vendor timescale
    # warning names our file as the includer, so a substring test filed 107 of
    # them as ours.
    loc = re.compile(r"^%(?:Warning-\w+|Error(?:-\w+)?):\s*([^:]+:[\\/][^:]*):")
    for line in out.splitlines():
        m = loc.match(line)
        if m:
            kind = line.split(":")[0]
            keep = m.group(1).startswith(mine)
            if keep:
                ours[kind] = ours.get(kind, 0) + 1
                show.append(line)
            else:
                vendor[kind] = vendor.get(kind, 0) + 1
        elif keep and line.startswith(" ") and not line.lstrip().startswith("..."):
            show.append(line)

    for line in show:
        print(line)
    print("\n--- OUR RTL ---")
    for k, n in sorted(ours.items(), key=lambda kv: -kv[1]) or [("clean", 0)]:
        print(f"{n:5d}  {k}")
    print("--- vendor (not ours to fix) ---")
    for k, n in sorted(vendor.items(), key=lambda kv: -kv[1]):
        print(f"{n:5d}  {k}")
    # OUR findings decide the exit code. Verilator's own status is non-zero for
    # a vendor file it cannot elaborate, which would make this permanently red
    # and therefore useless as a gate.
    return 1 if ours else 0


if __name__ == "__main__":
    sys.exit(main())
