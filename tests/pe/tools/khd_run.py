"""Generate and run the DSP datapath's component test, and name what failed.

    python tests/pe/tools/khd_run.py
    python tests/pe/tools/khd_run.py --simd 4 --muls 2 --no-perm

The bench reports a failing write by case and instruction index, because that is
what Verilog can cheaply carry; this joins it to the mnemonic from the
generator's own record, so a failure reads as "vmin.s8 at instruction 18" rather
than as a number.

Every configuration is generated AS ITSELF: the flags pick both the RTL
parameters and the instruction pool, so a build without the permute unit is
verified on a stream that never uses one, and a build with it is verified on a
stream that does.
"""

import argparse
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
TOOLS = ROOT / "tests" / "pe" / "tools"
XSIM = ROOT / "scripts" / "py" / "xsim.py"
BUILD = ROOT / "tests" / "pe" / "build" / "khd"

WFAIL = re.compile(r"FAIL case(\d+) write (\d+) \(instruction (\d+)\)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--simd", type=int, default=8, choices=(2, 4, 8))
    ap.add_argument("--muls", type=int, default=4, choices=(2, 4))
    ap.add_argument("--vregs", type=int, default=8)
    ap.add_argument("--nacc", type=int, default=2)
    ap.add_argument("--no-shift", action="store_true")
    ap.add_argument("--no-perm", action="store_true")
    ap.add_argument("--f16", action="store_true", help="build the float tier")
    ap.add_argument("--vreg-prim", default="distributed",
                    choices=("distributed", "block"))
    ap.add_argument("--wb-stage", type=int, default=0, choices=(0, 1))
    ap.add_argument("--wall", type=float, default=600.0)
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()

    tag = "s%d_m%d_v%d_a%d%s%s%s" % (a.simd, a.muls, a.vregs, a.nacc,
                                     "" if not a.no_shift else "_nosh",
                                     "" if not a.no_perm else "_nopm",
                                     "_f16" if a.f16 else "")

    gen = [sys.executable, str(TOOLS / "khd_gen.py"),
           "--simd", str(a.simd), "--muls", str(a.muls),
           "--vregs", str(a.vregs), "--nacc", str(a.nacc)]
    if a.no_shift:
        gen.append("--no-shift")
    if a.no_perm:
        gen.append("--no-perm")
    if a.f16:
        gen.append("--f16")
    if subprocess.run(gen, cwd=str(ROOT), check=False).returncode:
        return 1

    cmd = [sys.executable, str(XSIM), "khd_unit", "--wall", str(a.wall),
           "-d", "KHD_SIMD=%d" % a.simd,
           "-d", "KHD_MULS=%d" % a.muls,
           "-d", "KHD_VREGS=%d" % a.vregs,
           "-d", "KHD_NACC=%d" % a.nacc,
           "-d", "KHD_SHIFT=%d" % (0 if a.no_shift else 1),
           "-d", "KHD_PERM=%d" % (0 if a.no_perm else 1),
           "-d", "KHD_F16=%d" % (1 if a.f16 else 0),
           "-d", "KHD_WB=%d" % a.wb_stage]
    if a.vreg_prim == "block":
        cmd += ["-d", "KHD_RF_BRAM"]
    if a.keep:
        cmd.append("--keep")
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                       check=False)
    out = r.stdout
    print(out, end="")

    names = {}
    wn = BUILD / "cur" / "wnames.txt"
    if wn.exists():
        for ln in wn.read_text().splitlines():
            c, i, n = ln.split("\t")
            names[(int(c), int(i))] = n
    named = [(m.group(1), m.group(3), names.get((int(m.group(1)), int(m.group(2))), "?"))
             for m in WFAIL.finditer(out)]
    if named:
        print("\n  the failing writes, by mnemonic:")
        for c, ins, nm in named:
            print("    case%s instruction %s   %s" % (c, ins, nm))

    ok = "PASS --" in out and "FAIL" not in out
    print("\n  %s -- configuration %s" % ("PASS" if ok else "FAIL", tag))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
