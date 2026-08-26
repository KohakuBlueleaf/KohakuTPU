"""Run every SIMT PE shader through the machine bench and report one verdict.

    python tests/pe/tools/rv_simt_suite.py
    python tests/pe/tools/rv_simt_suite.py --only smoke diverge
    python tests/pe/tools/rv_simt_suite.py --gates          # ISA/model gates too

SERIAL BY CONSTRUCTION. `xsim.py` names its build directory after the bench, so
two concurrent runs of `kht_sys` destroy each other's work area -- which shows
up as a random shader failing rather than as a collision.

Each case names its own DRAM fill and launch count, because those are part of
what the shader proves: `gpu_gather` reads a ramp, `gpu_waves` is only a
dispatch test if more than one wave is launched, and `gpu_chain` is only G7's
witness when its 1-wave and 2-wave runs are compared.
"""

import argparse
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[3]
RUN = ROOT / "tests" / "pe" / "tools" / "rv_simt_run.py"
PROG = ROOT / "tests" / "pe" / "prog"

#: name -> (dram, launch, extra defines)
CASES = [
    ("smoke", "zero", 1, []),
    ("diverge", "zero", 1, []),
    ("nested", "zero", 1, []),
    ("gather", "ramp", 1, []),
    ("isa", "zero", 1, []),
    # The per-thread ALU. Everything else reaches only add/addi/andi/slli/sub on
    # it, so without this the eight lane datapaths are nine tenths untested.
    ("valu", "zero", 1, []),
    ("lds", "zero", 1, []),
    # The gate OFF is half of G4's witness: same shader, same answer, more
    # requests. A build that only ever runs the banked path proves neither.
    ("lds", "zero", 1, ["KHT_LDSBANK=0"]),
    # THE BANK COUNT IS A WIDTH: fewer banks is more conflicts, and kht_lds'
    # resolver already drains them. Same shader, same answer, more passes.
    ("lds", "zero", 1, ["KHT_LDSB=4"]),
    ("lds", "zero", 1, ["KHT_LDSB=2"]),
    ("lds", "zero", 1, ["KHT_LDSB=1"]),
    ("shfl", "zero", 1, []),
    # THE SHUFFLE WIDTH, same rule as FLANES and PERM_UNITS: one image, one
    # golden DRAM, fewer units and more passes. A build whose units serve the
    # wrong lanes is a wrong word, which is what simt_shfl.s already checks.
    ("shfl", "zero", 1, ["KHT_SHFLU=4"]),
    ("shfl", "zero", 1, ["KHT_SHFLU=2"]),
    ("shfl", "zero", 1, ["KHT_SHFLU=1"]),
    ("shfl", "zero", 16, ["KHT_SHFLU=2"]),
    ("waves", "zero", 1, []),
    ("waves", "zero", 16, []),
    ("chain", "zero", 1, []),
    ("chain", "zero", 2, []),
    ("fault", "zero", 1, []),
    # G9. One wave is the WORST case for the float tier, not the easy one: with
    # nothing else runnable the tier's latency is exposed rather than hidden, so
    # a dependent chain that is right here is right at any occupancy.
    # simt_f32 carries the two format witnesses: a 2^100 operand and a mantissa
    # bit only 24 significand bits keep.
    ("f32", "zero", 1, []),
    ("f32", "zero", 16, []),
    # THE PASS WALK. Per-lane distinct float operands, so a build whose units
    # serve the wrong threads is a wrong word rather than a pass -- which every
    # other float shader is, because their float operands are all uniform.
    # THE SAME IMAGE AT EVERY WIDTH is what makes "the ISA knows no unit count"
    # a test: only the generic changes, never the shader or the golden DRAM.
    ("fwalk", "zero", 1, []),
    ("fwalk", "zero", 16, []),
    ("fwalk", "zero", 1, ["KHT_FLANES=4"]),
    ("fwalk", "zero", 1, ["KHT_FLANES=2"]),
    ("fwalk", "zero", 1, ["KHT_FLANES=1"]),
    ("fwalk", "zero", 16, ["KHT_FLANES=2"]),
    ("f32", "zero", 16, ["KHT_FLANES=4"]),
    ("f32", "zero", 16, ["KHT_FLANES=2"]),
    # THE MULTIPLY IS NOT A WIDTH. A thread's ALU is an IM unit, so the multiply
    # count is LANES and there is no `KHT_MUL`. What still needs saying is that
    # `mul` does not depend on the float tier: this row runs it with no floats.
    ("mul", "zero", 16, ["KHT_FLANES=0"]),
    # RV32M. The sign corners are the point: mulh/mulhu/mulhsu are three
    # different high halves of the same two bit patterns.
    ("mul", "zero", 1, []),
    ("mul", "zero", 16, []),
]

GATES = [
    ["rv_simt_isa_test.py"],
    ["rv_simt_check.py"],
    ["rv_simt_emit.py", "--check"],
]


def label(name, launch, defines):
    s = "simt_%s" % name
    if launch != 1:
        s += " x%d" % launch
    if defines:
        s += " [" + ",".join(defines) + "]"
    return s


def run_case(name, dram, launch, defines, arg, wall):
    src = PROG / ("simt_%s.s" % name)
    if not src.is_file():
        return None, "no such shader: %s" % src
    cmd = [
        sys.executable,
        str(RUN),
        str(src),
        "--arg",
        hex(arg),
        "--dram",
        dram,
        "--launch",
        str(launch),
        "--wall",
        str(wall),
    ]
    for d in defines:
        cmd += ["-d", d]
    p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, check=False)
    out = p.stdout + p.stderr
    # The bench prints its own verdict; trust the return code and quote the
    # bench's line so a failure names itself instead of needing the log opened.
    line = ""
    for ln in out.splitlines():
        if "PASS" in ln or "FAIL" in ln:
            line = ln.strip()
    if not line:
        tail = out.strip().splitlines()
        line = tail[-1].strip() if tail else ""
    return p.returncode, line


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--only", nargs="*", default=None, help="shader short names, e.g. smoke diverge"
    )
    ap.add_argument("--arg", type=lambda s: int(s, 0), default=0x80000000)
    ap.add_argument("--wall", type=float, default=600.0)
    ap.add_argument(
        "--gates",
        action="store_true",
        help="also run the ISA, model and header-drift gates",
    )
    a = ap.parse_args()

    cases = CASES
    if a.only:
        want = set(a.only)
        cases = [c for c in CASES if c[0] in want]
        if not cases:
            print(
                "no case matches %s; known: %s"
                % (sorted(want), sorted({c[0] for c in CASES}))
            )
            return 2

    bad = 0
    if a.gates:
        for g in GATES:
            t0 = time.time()
            p = subprocess.run(
                [sys.executable, str(ROOT / "tests" / "pe" / "tools" / g[0])] + g[1:],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                check=False,
            )
            ok = p.returncode == 0
            bad += not ok
            print(
                "%-28s %-5s %5.1fs" % (g[0], "PASS" if ok else "FAIL", time.time() - t0)
            )

    for name, dram, launch, defines in cases:
        t0 = time.time()
        rc, line = run_case(name, dram, launch, defines, a.arg, a.wall)
        dt = time.time() - t0
        if rc is None:
            print("%-28s %-5s %s" % (label(name, launch, defines), "MISS", line))
            bad += 1
            continue
        ok = rc == 0
        bad += not ok
        print(
            "%-28s %-5s %5.1fs  %s"
            % (label(name, launch, defines), "PASS" if ok else "FAIL", dt, line)
        )

    print("---- %d case(s), %d failed" % (len(cases), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
