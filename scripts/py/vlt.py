"""Run a Verilog bench under Verilator, reusing xsim.py's build lists.

    python scripts/py/vlt.py fpacc
    python scripts/py/vlt.py cluster_node --keep
    python scripts/py/vlt.py --lint-only mm_mesh

BENCHES IS IMPORTED, NEVER COPIED. scripts/py/xsim.py stays the single source of
truth for what a bench is made of; this file only changes which simulator the
list is handed to. A source file added there reaches Verilator with no second
edit, which is the same reason xsim.py names benches instead of taking lists.

Verilator does not run natively on Windows in this checkout -- it is installed
inside WSL (Ubuntu-24.04, Verilator 5.020). Paths are translated on the way in;
`--native` uses a `verilator` on PATH instead, for a Linux checkout.

The XPM cells are SHIMMED, not taken from Vivado: sim/verilator/shims/ explains
why (Vivado's own xpm_memory.sv uses `deassign`, which Verilator rejects).
"""

import argparse
import importlib.util
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
SHIMS = ROOT / "sim/verilator/shims"
WSL_DISTRO = "Ubuntu-24.04"

# Warnings this repo's RTL trips wholesale and that no xsim run has ever gated
# on. Left ON as warnings by --warn: they are real lint findings (LATCH and
# WIDTHTRUNC in particular), just not ones to fail a migration run over.
SILENCED = [
    "LATCH",
    "WIDTHTRUNC",
    "WIDTHEXPAND",
    "PINMISSING",
    "TIMESCALEMOD",
    "INITIALDLY",
    "UNOPTFLAT",
    "CASEINCOMPLETE",
    "IMPLICIT",
    "SYNCASYNCNET",
    "MULTIDRIVEN",
    "BLKANDNBLK",
]


def load_xsim():
    spec = importlib.util.spec_from_file_location("xsim", ROOT / "scripts/py/xsim.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def to_wsl(p: pathlib.Path) -> str:
    """C:\\x\\y -> /mnt/c/x/y. WSL sees the Windows tree; nothing is copied."""
    s = pathlib.Path(p).resolve().as_posix()
    m = re.match(r"^([A-Za-z]):/(.*)$", s)
    return f"/mnt/{m.group(1).lower()}/{m.group(2)}" if m else s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bench")
    ap.add_argument("--lint-only", action="store_true")
    ap.add_argument("--native", action="store_true", help="verilator on PATH, not WSL")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--warn", action="store_true", help="show the silenced warnings")
    ap.add_argument("--define", "-d", action="append", default=[])
    ap.add_argument("--model", type=int, default=1, help="1 = behavioural, 0 = DSP48")
    # NOT 0 ("one per core"). This host has 172 logical CPUs and Verilator 5.020
    # aborts at exit with "attempted to destroy locked Thread Pool" at that width
    # -- after linking a good binary, so it reads as a build failure that is not.
    ap.add_argument("--jobs", "-j", type=int, default=8, help="verilator -j")
    ap.add_argument("--build-root", default=None)
    # The counterpart of xsim.py's --max-time. Verilator has no runtime time
    # bound, so the bound is elaborated: a wrapper top instantiates the bench
    # (bench tops have no ports) and calls $finish at the deadline. Both
    # simulators then cover the SAME simulated interval, which is the only way
    # the wall-clock numbers compare.
    ap.add_argument("--timebox", help="stop after this sim time, e.g. 200us")
    # --binary produces a standalone executable running a Verilog testbench, with
    # no way in from outside. --cc emits a C++ CLASS and the harness owns main(),
    # the clock and eval() -- which is what an interactive model, a driver
    # backend and differential testing against a golden ISA model all need.
    ap.add_argument("--cc", metavar="HARNESS.cpp", help="build a C++ model + harness")
    ap.add_argument("--trace", action="store_true", help="VCD; costs 10-100x")
    ap.add_argument("--run-args", default="", help="passed to the harness binary")
    args = ap.parse_args()

    if args.cc and args.lint_only:
        sys.exit("--cc and --lint-only are different jobs; pick one")
    harness = None
    if args.cc:
        harness = pathlib.Path(args.cc)
        if not harness.is_absolute():
            harness = ROOT / harness
        if not harness.exists():
            sys.exit(f"harness not found: {harness}")

    xsim = load_xsim()
    if args.bench not in xsim.BENCHES:
        sys.exit(f"unknown bench {args.bench!r}; xsim.py knows {len(xsim.BENCHES)}")
    top, srcs = xsim.BENCHES[args.bench]

    if args.model == 0:
        sys.exit(
            "--model 0 needs the Xilinx unisims (DSP48E2). Not wired up: see\n"
            "sim/verilator/README.md, 'What stays on xsim'."
        )

    root = pathlib.Path(args.build_root) if args.build_root else ROOT / "build"
    if not root.is_absolute():
        root = ROOT / root
    work = root / f"vlt_{args.bench}"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    # Deduped, order kept -- same rule as xsim.py: a file named twice is a
    # duplicate module definition and the build is rejected.
    files = [ROOT / p for p in dict.fromkeys(srcs)]
    missing = [f for f in files if not f.exists()]
    if missing:
        for f in missing:
            print(f"  MISSING {f}")
        sys.exit(f"{len(missing)} source file(s) in BENCHES do not exist")

    # PE_DIR ABSOLUTE, through a FILE, exactly as xsim.py does it -- the nine PE
    # benches default it to "../../tests/pe/build", which resolves from the run
    # directory and so is only correct at the default build root. Without this
    # rv_core reports "no cases -- run the generator" while the images are there.
    predef = work / "kohaku_predef.vh"
    predef.write_text(
        f'`define PE_DIR "{(ROOT / "tests/pe/build").as_posix()}"\n', encoding="utf-8"
    )

    # The shims go FIRST so they win module lookup before any -I directory is
    # searched for a same-named file.
    lines = [to_wsl(p) for p in sorted(SHIMS.glob("*.v"))]
    lines += [to_wsl(predef)]
    lines += [f"-I{to_wsl(ROOT / d)}" for d in xsim.INCDIRS]
    lines += [to_wsl(p) for p in files]

    if args.timebox:
        box = work / "vlt_timebox.v"
        box.write_text(
            "`timescale 1ns/1ps\n"
            "module vlt_timebox;\n"
            f"    {top} u_tb();\n"
            "    initial begin\n"
            f"        #{args.timebox};\n"
            f'        $display("@@@ TIMEBOX {args.timebox} reached");\n'
            "        $finish;\n"
            "    end\n"
            "endmodule\n",
            encoding="utf-8",
        )
        lines += [to_wsl(box)]
        top = "vlt_timebox"

    (work / "vlt.f").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # MATCHES xelab's `-timescale 1ns/1ps`. Most RTL here carries no `timescale`
    # of its own (Verilator reports TIMESCALEMOD by the dozen), and without this
    # those modules take Verilator's default instead of the bench's unit.
    cmd = ["verilator", "-sv", "--timing", "-Wno-fatal", "--timescale", "1ns/1ps"]
    if args.lint_only:
        cmd += ["--lint-only"]
    elif harness:
        cmd += ["--cc", "--exe", "--build", "-o", "vsim"]
        cmd += ["-CFLAGS", "-O2 -std=c++17"]
        if args.trace:
            cmd += ["--trace"]
    else:
        cmd += ["--binary", "-o", "vsim"]
    cmd += ["-j", str(args.jobs)] if not args.lint_only else []
    if not args.warn:
        cmd += [f"-Wno-{w}" for w in SILENCED]
    cmd += [f"+define+{d}" for d in args.define + [f"MX_MODEL={args.model}"]]
    cmd += ["--top-module", top, "-f", "vlt.f"]
    if harness:
        cmd += [to_wsl(harness)]

    t_build = time.monotonic()
    wwork = to_wsl(work)
    # CAPTURED, not streamed: a --binary build prints one g++ line per translation
    # unit and buries anything worth reading. Errors are re-printed below.
    if args.native:
        bp = subprocess.run(cmd, cwd=work, check=False, capture_output=True, text=True)
    else:
        bp = subprocess.run(
            [
                "wsl",
                "-d",
                WSL_DISTRO,
                "--",
                "bash",
                "-lc",
                # QUOTED, not joined: -CFLAGS takes ONE argument, and a bare
                # join hands `-std=c++17` to verilator as its own option.
                f"cd {wwork} && " + shlex.join(cmd),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    rc = bp.returncode
    blog = (bp.stdout or "") + (bp.stderr or "")
    (work / "build.log").write_text(blog, encoding="utf-8", errors="replace")
    for ln in blog.splitlines():
        if ln.startswith("%Error"):
            print(ln)
    t_build = time.monotonic() - t_build
    # A linked binary outranks the exit code, for the thread-pool abort above.
    if rc and not (work / "obj_dir" / "vsim").exists():
        return rc
    if args.lint_only:
        print(f"  LINT OK -- {top}  ({t_build:.1f}s)")
        return 0

    inv = f"./obj_dir/vsim {args.run_args}".strip()
    run = ["wsl", "-d", WSL_DISTRO, "--", "bash", "-lc", f"cd {wwork} && {inv}"]
    if args.native:
        run = [str(work / "obj_dir/vsim")] + args.run_args.split()
    t_run = time.monotonic()
    rp = subprocess.run(run, capture_output=True, text=True, check=False)
    out = rp.stdout + (rp.stderr or "")
    t_run = time.monotonic() - t_run
    print(out)
    print(f"  @@@ TIMING build {t_build:.2f}s  run {t_run:.2f}s")

    # A harness build is meant to be KEPT and re-driven -- rebuilding a C++ model
    # per run is the one cost that makes an interactive model pointless.
    if not args.keep and not harness:
        shutil.rmtree(work, ignore_errors=True)
    if harness:
        print(f"  model at {work / 'obj_dir' / 'vsim'}")
        return rp.returncode
    verdicts = [ln.strip() for ln in out.splitlines()]
    passed = any(v.startswith("PASS") for v in verdicts)
    failed = any(v.startswith("FAIL") for v in verdicts)
    return 0 if passed and not failed else 1


if __name__ == "__main__":
    sys.exit(main())
