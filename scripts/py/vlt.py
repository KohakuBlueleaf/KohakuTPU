"""Run a Verilog bench under Verilator, reusing xsim.py's build lists.

    python scripts/py/vlt.py fpacc
    python scripts/py/vlt.py cluster_node --keep
    python scripts/py/vlt.py --lint-only mm_mesh

BENCHES IS IMPORTED, NEVER COPIED. scripts/py/xsim.py stays the single source of
truth for what a bench is made of; this file only changes which simulator the
list is handed to. A source file added there reaches Verilator with no second
edit, which is the same reason xsim.py names benches instead of taking lists.

By default Verilator runs inside WSL (Ubuntu-24.04, Verilator 5.020), paths
translated on the way in. `--native` runs the one on PATH instead: on Linux
`verilator`, on Windows conda-forge's `verilator_bin.exe` with `make` and a
mingw `g++` on PATH -- its verilated.mk names MSVC, so the toolchain is passed
to make (`VLT_CXX` / `VLT_AR` override g++ / ar).

The XPM cells are SHIMMED, not taken from Vivado: sim/verilator/shims/ explains
why (Vivado's own xpm_memory.sv uses `deassign`, which Verilator rejects).

Every module with a wrapper in sim/verilator/models/ is built under a
quiescence clock gate unless `--rtl` is given: its source file is copied with
the module renamed `<name>__rtl` and the wrapper takes the name. The RTL is read,
never edited. sim/verilator/docs/models.md has the exactness argument.
"""

import argparse
import importlib.util
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
SHIMS = ROOT / "sim/verilator/shims"
MODELS = ROOT / "sim/verilator/models"
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


def to_native(p: pathlib.Path) -> str:
    """The path as a native simulator sees it."""
    return pathlib.Path(p).resolve().as_posix()


def opt_make(fast: str) -> str:
    """verilated.mk's optimisation levels: OPT_FAST for the per-cycle code (its
    own default is -Os), the run-once constructors at -O1."""
    return f"OPT_FAST={fast} OPT_GLOBAL=-O3 OPT_SLOW=-O1"


def native_cmd(fast: str) -> list:
    """The native Verilator and, on Windows, the toolchain its make must use."""
    if os.name != "nt":
        return ["verilator", "-MAKEFLAGS", opt_make(fast)]
    exe = shutil.which("verilator_bin")
    if exe is None:
        sys.exit("--native on Windows needs verilator_bin.exe on PATH (conda-forge)")
    cxx = os.environ.get("VLT_CXX", "g++")
    mk = [
        f"CXX={cxx}",
        f"LINK={cxx}",
        f"AR={os.environ.get('VLT_AR', 'ar')}",
        f"PYTHON3={pathlib.Path(sys.executable).as_posix()}",
        # --timing benches are C++20 coroutines.
        "CFG_CXXFLAGS_STD=-std=gnu++20",
        "CFG_CXXFLAGS_COROUTINES=-fcoroutines",
        "CFG_CXXFLAGS_PCH_I=-include",
        "CFG_LDLIBS_THREADS=-pthread",
        opt_make(fast),
    ]
    # STATIC: the shared libstdc++ runs thread_local destructors after the
    # thread pool is gone and faults at exit, and PATH picks which DLL loads.
    return [exe, "-MAKEFLAGS", " ".join(mk), "-LDFLAGS", "-static"]


def module_decl(name: str) -> re.Pattern:
    """A line declaring module `name` (comments start with `//`, so never match)."""
    return re.compile(rf"^(\s*module\s+){name}\b", re.MULTILINE)


def apply_models(files: list, work: pathlib.Path) -> tuple:
    """`files` with every modelled module's source swapped for a renamed copy,
    and the wrappers and their library ahead of them. Also the copied files'
    original directories, for their relative `include`s."""
    wrappers = {p.stem: p for p in sorted(MODELS.glob("*.v"))}
    out, used, incdirs = [], [], []
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        hit = [n for n in wrappers if module_decl(n).search(text)]
        if not hit:
            out.append(f)
            continue
        for n in hit:
            text = module_decl(n).sub(rf"\g<1>{n}__rtl", text)
        copy = work / "rtl" / f.name
        copy.parent.mkdir(exist_ok=True)
        copy.write_text(text, encoding="utf-8")
        out.append(copy)
        used += hit
        incdirs.append(f.parent)
    if not used:
        return files, []
    lib = sorted((MODELS / "lib").glob("*.v"))
    return lib + [wrappers[n] for n in used] + out, incdirs


def model_benches(xsim) -> dict:
    """The models' differential benches: each gated wrapper beside its own
    free-running `__rtl` copy. Verilator only -- xsim has no `__rtl` module."""
    tests = "sim/verilator/models/tests"
    alu = [s for s in xsim.BENCHES["vec_alu"][1] if not s.startswith("tests/")]
    lanes = [s for s in xsim.BENCHES["vec_lanes"][1] if not s.startswith("tests/")]
    mm = xsim.COMMON + xsim.MATMUL
    return {
        "gate_lanes": ("gate_lanes_tb", lanes + [f"{tests}/gate_lanes_tb.v"]),
        "gate_core": ("gate_core_tb", mm + [f"{tests}/gate_core_tb.v"]),
        "gate_acu": ("gate_acu_tb", mm + [f"{tests}/gate_acu_tb.v"]),
        "gate_acu_pump": ("gate_acu_pump_tb", mm + [f"{tests}/gate_acu_pump_tb.v"]),
        "gate_vec_alu": ("gate_vec_alu_tb", alu + [f"{tests}/gate_vec_alu_tb.v"]),
    }


def vsim_of(work: pathlib.Path) -> pathlib.Path:
    """The linked model: `vsim`, or `vsim.exe` from a Windows toolchain."""
    exe = work / "obj_dir" / "vsim.exe"
    return exe if exe.exists() else work / "obj_dir" / "vsim"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bench")
    ap.add_argument("--lint-only", action="store_true")
    ap.add_argument("--native", action="store_true", help="verilator on PATH, not WSL")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--warn", action="store_true", help="show the silenced warnings")
    ap.add_argument("--define", "-d", action="append", default=[])
    # Top-level parameter overrides (verilator -G): a lint-only entry whose top
    # is an RTL module has no bench to set MODEL from MX_MODEL.
    ap.add_argument("--gparam", "-G", action="append", default=[], help="NAME=VALUE")
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
    ap.add_argument(
        "--opt-fast", default="-O3", help="OPT_FAST, the per-cycle code's -O"
    )
    # No -O here: these reach EVERY file, and an -O3 would override OPT_SLOW for
    # the run-once constructors. The levels are opt_make's.
    ap.add_argument(
        "--cxx-opt",
        default="-march=native",
        help="C++ flags of a --cc model, added to every file",
    )
    ap.add_argument("--run-args", default="", help="passed to the harness binary")
    ap.add_argument("--vlt-config", action="append", default=[], help="a .vlt file")
    ap.add_argument("--vflag", action="append", default=[], help="a raw verilator flag")
    ap.add_argument(
        "--rtl", action="store_true", help="no sim/verilator/models wrappers"
    )
    # Profile-guided C++: `gen` instruments the model, a run of it writes the
    # profile to <build-root>/pgo/<bench> (outside the work directory, which
    # every build removes), and `use` rebuilds from that profile.
    ap.add_argument("--pgo", choices=["gen", "use"], help="profile-guided --cc build")
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

    # The path form the simulator sees: WSL's /mnt view, or the native one.
    to_sim = to_native if args.native else to_wsl

    xsim = load_xsim()
    benches = {**xsim.BENCHES, **model_benches(xsim)}
    if args.bench not in benches:
        sys.exit(f"unknown bench {args.bench!r}; xsim.py knows {len(xsim.BENCHES)}")
    if args.rtl and args.bench in model_benches(xsim):
        sys.exit(f"{args.bench} compares against the models; --rtl removes them")
    top, srcs = benches[args.bench]

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

    model_inc = []
    if not args.rtl:
        files, model_inc = apply_models(files, work)

    # The shims go FIRST so they win module lookup before any -I directory is
    # searched for a same-named file.
    lines = [to_sim(p) for p in sorted(SHIMS.glob("*.v"))]
    lines += [to_sim(predef)]
    lines += [f"-I{to_sim(ROOT / d)}" for d in xsim.INCDIRS]
    lines += [f"-I{to_sim(d)}" for d in dict.fromkeys(model_inc)]
    lines += [to_sim(p) for p in files]

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
        lines += [to_sim(box)]
        top = "vlt_timebox"

    (work / "vlt.f").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # MATCHES xelab's `-timescale 1ns/1ps`. Most RTL here carries no `timescale`
    # of its own (Verilator reports TIMESCALEMOD by the dozen), and without this
    # those modules take Verilator's default instead of the bench's unit.
    # --cc: the harness owns time, so RTL `#` delays are not scheduled.
    timing = "--no-timing" if harness else "--timing"
    head = (
        native_cmd(args.opt_fast)
        if args.native
        else ["verilator", "-MAKEFLAGS", opt_make(args.opt_fast)]
    )
    cmd = head + ["-sv", timing, "-Wno-fatal", "--timescale", "1ns/1ps"]
    if args.lint_only:
        cmd += ["--lint-only"]
    elif harness:
        cmd += ["--cc", "--exe", "--build", "-o", "vsim"]
        cxx = args.cxx_opt
        if args.pgo:
            pgo = root / "pgo" / args.bench
            pgo.mkdir(parents=True, exist_ok=True)
            if args.pgo == "gen":
                cxx += f" -fprofile-generate={to_sim(pgo)} -fprofile-update=single"
                cmd += ["-LDFLAGS", "-fprofile-generate"]
            else:
                if not any(pgo.rglob("*.gcda")):
                    sys.exit(
                        f"--pgo use: no profile under {pgo}; build --pgo gen and run it"
                    )
                cxx += f" -fprofile-use={to_sim(pgo)} -fprofile-partial-training -Wno-missing-profile"
        # VTOP: the model class the harness includes.
        cmd += ["-CFLAGS", f"{cxx} -std=c++17 -DVTOP=V{top}"]
        if args.trace:
            cmd += ["--trace"]
    else:
        cmd += ["--binary", "-o", "vsim"]
    cmd += ["-j", str(args.jobs)] if not args.lint_only else []
    if not args.warn:
        cmd += [f"-Wno-{w}" for w in SILENCED]
    cmd += [f"+define+{d}" for d in args.define + [f"MX_MODEL={args.model}"]]
    cmd += [f"-G{g}" for g in args.gparam]
    cmd += args.vflag
    cmd += [to_sim(ROOT / v) for v in args.vlt_config]
    cmd += ["--top-module", top, "-f", "vlt.f"]
    if harness:
        cmd += [to_sim(harness)]

    t_build = time.monotonic()
    wwork = to_wsl(work)
    # CAPTURED, not streamed: a --binary build prints one g++ line per translation
    # unit and buries anything worth reading. Errors are re-printed below.
    if args.native:
        bp = subprocess.run(
            cmd,
            cwd=work,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
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
            encoding="utf-8",
            errors="replace",
        )
    rc = bp.returncode
    blog = (bp.stdout or "") + (bp.stderr or "")
    (work / "build.log").write_text(blog, encoding="utf-8", errors="replace")
    for ln in blog.splitlines():
        if ln.startswith("%Error"):
            print(ln)
    t_build = time.monotonic() - t_build
    # A linked binary outranks the exit code, for the thread-pool abort above.
    if rc and not vsim_of(work).exists():
        return rc
    if args.lint_only:
        print(f"  LINT OK -- {top}  ({t_build:.1f}s)")
        return 0

    inv = f"./obj_dir/vsim {args.run_args}".strip()
    run = ["wsl", "-d", WSL_DISTRO, "--", "bash", "-lc", f"cd {wwork} && {inv}"]
    if args.native:
        run = [str(vsim_of(work))] + args.run_args.split()
    t_run = time.monotonic()
    # utf-8 explicitly: the console codepage (cp950 here) cannot decode a
    # non-ASCII byte in the simulator's output and the whole run's text is lost
    rp = subprocess.run(
        run,
        cwd=work,
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
        errors="replace",
    )
    out = rp.stdout + (rp.stderr or "")
    t_run = time.monotonic() - t_run
    print(out)
    # a model that dies (signal, abort) prints nothing; say so, loudly
    if rp.returncode != 0:
        print(f"  @@@ RUN EXIT {rp.returncode} -- the model did not finish normally")
    print(f"  @@@ TIMING build {t_build:.2f}s  run {t_run:.2f}s")

    # A harness build is meant to be KEPT and re-driven -- rebuilding a C++ model
    # per run is the one cost that makes an interactive model pointless.
    if not args.keep and not harness:
        shutil.rmtree(work, ignore_errors=True)
    if harness:
        print(f"  model at {vsim_of(work)}")
        return rp.returncode
    verdicts = [ln.strip() for ln in out.splitlines()]
    passed = any(v.startswith("PASS") for v in verdicts)
    failed = any(v.startswith("FAIL") for v in verdicts)
    return 0 if passed and not failed else 1


if __name__ == "__main__":
    sys.exit(main())
