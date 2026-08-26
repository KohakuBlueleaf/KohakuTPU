"""Generate and run the SIMD datapath's component test, and name what failed.

    python tests/pe/tools/khs_run.py
    python tests/pe/tools/khs_run.py --simd 4 --ilanes 2 --perm-units 0

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
    ap.add_argument("--vregs", type=int, default=8)
    ap.add_argument("--nacc", type=int, default=2)
    # THE COMPUTE WIDTHS. 0 is NOT BUILT and its encodings leave the stream; a
    # nonzero value below full changes only the cycle count, so the same
    # vectors grade every width of a feature that IS built.
    # None = FULL RATE FOR THIS VECTOR. A hard 8 made `--simd 4` ask for more
    # units than elements, which the elaboration guard refuses.
    ap.add_argument("--ilanes", type=int, default=None, choices=(0, 1, 2, 4, 8))
    ap.add_argument("--red", type=int, default=1, choices=(0, 1))
    # int32 <-> binary32 converter units; 0 faults the whole FCVT group.
    ap.add_argument("--fcvt-units", type=int, default=0, choices=(0, 1, 2, 4, 8))
    ap.add_argument(
        "--float", action="store_true", dest="flt", help="build the float tier"
    )
    # ARCHITECTURAL, so the generator and the bench must be told the SAME
    # number: fewer lanes is a shorter partial chain per element and therefore a
    # different answer. The bench refuses vectors built for another count.
    ap.add_argument(
        "--flanes",
        type=int,
        default=None,
        help="float units built; 0 = none, default SIMD",
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
    # NOT architectural: the permute's answer is the same at every count, only
    # the cycles change, so the same stream grades every width.
    ap.add_argument(
        "--perm-units",
        type=int,
        default=None,
        choices=(0, 1, 2, 4, 8),
        help="permute output words per pass; 0 = NOT BUILT",
    )
    # NOT architectural either: the shift's answer is the same at every count.
    ap.add_argument(
        "--shift-units",
        type=int,
        default=None,
        choices=(0, 1, 2, 4, 8),
        help="packed-shift units per pass; 0 = NOT BUILT",
    )
    ap.add_argument(
        "--vreg-prim", default="distributed", choices=("distributed", "block")
    )
    ap.add_argument("--wb-stage", type=int, default=0, choices=(0, 1))
    ap.add_argument("--wall", type=float, default=600.0)
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    for w in ("ilanes", "shift_units", "perm_units"):
        if getattr(a, w) is None:
            setattr(a, w, a.simd)
        n = getattr(a, w)
        if n > a.simd:
            ap.error(
                "--%s %d exceeds --simd %d: a width is units PER PASS over the "
                "vector, so it cannot be wider than the vector."
                % (w.replace("_", "-"), n, a.simd)
            )
    if a.flanes is None:
        a.flanes = a.simd
    if a.flt and a.flanes == 0:
        ap.error(
            "--float with --flanes 0: 0 means the tier is NOT built. "
            "Use --flanes %d for the widest float tier." % a.simd
        )

    tag = "s%d_il%d_sh%d_pm%d_r%d_v%d_a%d%s" % (
        a.simd,
        a.ilanes,
        a.shift_units,
        a.perm_units,
        a.red,
        a.vregs,
        a.nacc,
        "_float" if a.flt else "",
    )

    gen = [
        sys.executable,
        str(TOOLS / "khs_gen.py"),
        "--simd",
        str(a.simd),
        "--vregs",
        str(a.vregs),
        "--nacc",
        str(a.nacc),
        "--ilanes",
        str(a.ilanes),
        "--shiftu",
        str(a.shift_units),
        "--permu",
        str(a.perm_units),
        "--red",
        str(a.red),
        "--fcvtu",
        str(a.fcvt_units),
    ]
    if a.flt:
        gen.append("--float")
    if a.no_falu:
        gen.append("--no-falu")
    if a.no_facc:
        gen.append("--no-facc")
    if a.fsfu == 0:
        gen.append("--no-fsfu")
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
        "KHS_VREGS=%d" % a.vregs,
        "-d",
        "KHS_NACC=%d" % a.nacc,
        "-d",
        "KHS_ILANES=%d" % a.ilanes,
        # DERIVED, NOT A KNOB HERE: the round adder lives INSIDE the shifter, so
        # asking for it with no shifter is refused at elaboration. The bench
        # would otherwise fail to build rather than report a configuration.
        "-d",
        "KHS_SHROUND=%d" % (1 if a.shift_units else 0),
        "-d",
        "KHS_PERMU=%d" % a.perm_units,
        "-d",
        "KHS_SHIFTU=%d" % a.shift_units,
        "-d",
        "KHS_RED=%d" % a.red,
        "-d",
        "KHS_FCVTU=%d" % a.fcvt_units,
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
