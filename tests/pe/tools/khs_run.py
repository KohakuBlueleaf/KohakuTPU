"""Generate and run the SIMD datapath's component test, and name what failed.

    python tests/pe/tools/khs_run.py
    python tests/pe/tools/khs_run.py --simd 4 --muls 2 --no-perm

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
    ap.add_argument(
        "--float", action="store_true", dest="flt", help="build the float tier"
    )
    # ARCHITECTURAL, so the generator and the bench must be told the SAME
    # number: fewer lanes is a shorter partial chain per element and therefore a
    # different answer. The bench refuses vectors built for another count.
    # DEFAULTS TO 2*SIMD, NOT TO 0: 0 now means the float tier is not built.
    ap.add_argument(
        "--flanes",
        type=int,
        default=None,
        help="float units built; 0 = none, default 2*SIMD",
    )
    # NOT architectural, unlike --flanes: an elementwise seed's result depends on
    # its own element and nothing else, so the SAME vectors must pass at every
    # count. That is what makes this the walk's test rather than a rebuild.
    ap.add_argument(
        "--fsfu",
        type=int,
        default=1,
        help="seed units built; 0 = none, and a seed then faults",
    )
    # The GROUPS, forwarded to the generator as well as to the bench: khs_gen
    # already had these three and nothing here passed them, so a run could not
    # isolate the elementwise tier from the accumulator.
    ap.add_argument("--no-falu", action="store_true")
    ap.add_argument("--no-facc", action="store_true")
    # NOT architectural: an element's answer does not depend on which OTHER
    # formats the build carries, so the same vectors must pass with either off.
    ap.add_argument("--no-f16", action="store_true")
    ap.add_argument("--no-f32", action="store_true")
    # NOT architectural: the permute's answer is the same at every count, only
    # the cycles change, so the same stream grades every width.
    ap.add_argument(
        "--perm-units",
        type=int,
        default=0,
        help="permute output words per pass; 0 = one per word",
    )
    # NOT architectural either: the shift's answer is the same at every count.
    ap.add_argument(
        "--shift-units",
        type=int,
        default=0,
        help="packed-shift units per pass; 0 = one per lane",
    )
    ap.add_argument(
        "--vreg-prim", default="distributed", choices=("distributed", "block")
    )
    ap.add_argument("--wb-stage", type=int, default=0, choices=(0, 1))
    ap.add_argument("--wall", type=float, default=600.0)
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    if a.flanes is None:
        a.flanes = 2 * a.simd
    if a.flt and a.flanes == 0:
        ap.error(
            "--float with --flanes 0: 0 means the tier is NOT built. "
            "Use --flanes %d for one unit per element." % (2 * a.simd)
        )

    tag = "s%d_m%d_v%d_a%d%s%s%s" % (
        a.simd,
        a.muls,
        a.vregs,
        a.nacc,
        "" if not a.no_shift else "_nosh",
        "" if not a.no_perm else "_nopm",
        "_float" if a.flt else "",
    )

    gen = [
        sys.executable,
        str(TOOLS / "khs_gen.py"),
        "--simd",
        str(a.simd),
        "--muls",
        str(a.muls),
        "--vregs",
        str(a.vregs),
        "--nacc",
        str(a.nacc),
    ]
    if a.no_shift:
        gen.append("--no-shift")
    if a.no_perm:
        gen.append("--no-perm")
    if a.flt:
        gen.append("--float")
    if a.no_falu:
        gen.append("--no-falu")
    if a.no_facc:
        gen.append("--no-facc")
    if a.fsfu == 0:
        gen.append("--no-fsfu")
    if a.no_f16:
        gen.append("--no-f16")
    if a.no_f32:
        gen.append("--no-f32")
    gen += ["--flanes", str(a.flanes)]
    if subprocess.run(gen, cwd=str(ROOT), check=False).returncode:
        return 1

    cmd = [
        sys.executable,
        str(XSIM),
        "khs_unit",
        "--wall",
        str(a.wall),
        "-d",
        "KHS_SIMD=%d" % a.simd,
        "-d",
        "KHS_MULS=%d" % a.muls,
        "-d",
        "KHS_VREGS=%d" % a.vregs,
        "-d",
        "KHS_NACC=%d" % a.nacc,
        "-d",
        "KHS_SHIFT=%d" % (0 if a.no_shift else 1),
        "-d",
        "KHS_PERM=%d" % (0 if a.no_perm else 1),
        "-d",
        "KHS_PERMU=%d" % a.perm_units,
        "-d",
        "KHS_SHIFTU=%d" % a.shift_units,
        "-d",
        "KHS_FLOAT=%d" % (1 if a.flt else 0),
        "-d",
        "KHS_FLANES=%d" % a.flanes,
        "-d",
        "KHS_FSFU=%d" % a.fsfu,
        "-d",
        "KHS_FALU=%d" % (0 if a.no_falu else 1),
        "-d",
        "KHS_FACC=%d" % (0 if a.no_facc else 1),
        "-d",
        "KHS_F16=%d" % (0 if a.no_f16 else 1),
        "-d",
        "KHS_F32=%d" % (0 if a.no_f32 else 1),
        "-d",
        "KHS_WB=%d" % a.wb_stage,
    ]
    if a.vreg_prim == "block":
        cmd += ["-d", "KHS_RF_BRAM"]
    if a.keep:
        cmd.append("--keep")
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, check=False)
    out = r.stdout
    print(out, end="")

    names = {}
    wn = BUILD / "cur" / "wnames.txt"
    if wn.exists():
        for ln in wn.read_text().splitlines():
            c, i, n = ln.split("\t")
            names[(int(c), int(i))] = n
    named = [
        (m.group(1), m.group(3), names.get((int(m.group(1)), int(m.group(2))), "?"))
        for m in WFAIL.finditer(out)
    ]
    if named:
        print("\n  the failing writes, by mnemonic:")
        for c, ins, nm in named:
            print("    case%s instruction %s   %s" % (c, ins, nm))

    ok = "PASS --" in out and "FAIL" not in out
    print("\n  %s -- configuration %s" % ("PASS" if ok else "FAIL", tag))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
