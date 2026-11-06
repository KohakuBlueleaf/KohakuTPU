"""The test runner: stages picked by what changed, each held to its budget.

Each stage checks one compiler or firmware step by its outcome, so a change
reruns only the stages whose sources it touches. Every suite writes its result
under build/tests/ and a suite or a test over its budget is a failure.

    python tests/run.py                   # the stages this tree changes
    python tests/run.py --all             # every stage that needs no model
    python tests/run.py static stages/l1_l0
    python tests/run.py --all --mutants   # and every stage against its mutants
    python tests/run.py stages/l2_l1 --mutants 3/10   # the fourth of ten shards
    python tests/run.py --python-mutants 3/10         # the Python tiers, spec build
    python tests/run.py --emu-mutants                 # ka-emu against firmware/emu
    python tests/run.py --list

KOS_BUILD holds the core models, KTPU_SIM_VSIM the replay bench, KTPU_ANCHOR_CARD
a card model, KOS_LLVM a clang that reaches riscv64.
"""

import argparse
import dataclasses
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from typing import cast

SOFTWARE = pathlib.Path(__file__).resolve().parents[1]
ROOT = SOFTWARE.parent
REPORTS = ROOT / "build" / "tests"
#: At most this many jobs at once; `--jobs` or fewer cores lowers it.
JOBS = 16
#: Every suite that needs no model, seconds.
ALL_BUDGET = 120.0


@dataclasses.dataclass(frozen=True)
class Suite:
    """One stage: `name`, the tests' directory under tests/, its cargo tests
    `((package, target, ...), ...)`, its budgets in seconds, the sources that
    select it, the environment it needs. `default` needs no Verilated model."""

    name: str
    sources: tuple
    budget: float = 30.0
    per_test: float = 1.0
    cargo: tuple = ()
    pytest: bool = True
    default: bool = True
    env: tuple = ()


#: Mutants of the tracer, the generators and the runtime, and the suites that
#: can tell them, cheapest first.
PYTHON_MUTANTS = (
    "dsl.",
    "graph.",
    "api.",
    "kernels.",
    "app.",
    "bindings.",
    "expr/",
    "fused/",
    "prepared/",
    "pipe/",
    "rows/",
    "relayout/",
    "vector/",
)
AUDITED = ("logic", "stages/dsl", "stages/tinygrad", "runtime")
COMPILER = "kohakutpu/compiler/crates"
FRAMEWORK = "kohakuaccel/compiler/crates"
EMU_ENV = (
    ("KA_EMU_MODEL", str(ROOT / "build" / "emu")),
    ("KA_EMU_FIRMWARE", str(ROOT / "build" / "fw")),
    ("KOS_IMAGE", str(ROOT / "build" / "os" / "kos.elf")),
)
SUITES = (
    Suite(
        "static",
        ("kohakuaccel", "kohakutpu", "template", "tests/static", "pyproject.toml"),
        budget=30.0,
        per_test=20.0,
    ),
    Suite(
        "stages/l1_l0",
        (
            f"{COMPILER}/ktpu-l1",
            f"{COMPILER}/ktpu-l0",
            f"{COMPILER}/ktpu-verify",
            f"{COMPILER}/ktpu-isa",
            f"{FRAMEWORK}/ka-package",
        ),
        cargo=(("ktpu-sim", "l1_l0"),),
        pytest=False,
    ),
    Suite(
        "stages/l1_schedule",
        (f"{COMPILER}/ktpu-l1/src/sched", f"{COMPILER}/ktpu-l1/src/text"),
        cargo=(("ktpu-sim", "l1_schedule"),),
        pytest=False,
    ),
    Suite(
        "stages/l2_l1",
        (f"{COMPILER}/ktpu-l2", f"{COMPILER}/ktpu-sim"),
        cargo=(("ktpu-sim", "l2_l1"),),
        pytest=False,
    ),
    Suite(
        "stages/l2_passes",
        (f"{COMPILER}/ktpu-l2/src/passes",),
        cargo=(("ktpu-sim", "l2_passes"),),
        pytest=False,
    ),
    Suite(
        "stages/dsl",
        ("kohakutpu/language", f"{COMPILER}/ktpu-kernels", f"{COMPILER}/ktpu-graph"),
    ),
    Suite(
        "stages/tinygrad",
        ("kohakutpu/application/kohakutpu/application/ktpugrad",),
    ),
    Suite(
        "logic",
        (
            "kohakutpu/application/kohakutpu/application/runtime",
            "kohakutpu/application/kohakutpu/application/ktpugrad/captured",
            "kohakuaccel/driver",
            f"{COMPILER}/ktpu-py",
            f"{FRAMEWORK}/ka-debug",
        ),
    ),
    Suite(
        "runtime",
        (
            "kohakutpu/application/kohakutpu/application/runtime",
            "kohakutpu/application/kohakutpu/application/api",
        ),
        per_test=20.0,
    ),
    Suite(
        "firmware/host",
        ("kohakuaccel/firmware", "kohakuaccel/os", "kohakutpu/firmware"),
    ),
    Suite(
        "firmware/emu",
        ("kohakuaccel/firmware", "kohakuaccel/os", f"{FRAMEWORK}/ka-emu"),
        cargo=(
            ("ka-emu", "firmware_hart", "firmware_probe", "firmware_queue"),
            ("ktpu-sim", "firmware_package"),
        ),
        env=EMU_ENV,
    ),
    Suite(
        "anchors",
        (f"{COMPILER}/ktpu-sim",),
        budget=60.0,
        per_test=15.0,
        cargo=(("ktpu-sim", "anchors_core"),),
        default=False,
    ),
    Suite(
        "machine",
        ("kohakuaccel/firmware", "kohakuaccel/os"),
        budget=60.0,
        per_test=15.0,
        default=False,
    ),
    Suite(
        "perf",
        ("tests/perf", f"{COMPILER}/ktpu-l1/src/sched"),
        per_test=5.0,
    ),
    Suite(
        "e2e",
        (),
        budget=900.0,
        per_test=300.0,
        default=False,
    ),
)


def changed() -> list[str]:
    """Paths under software/ that differ from HEAD, untracked ones included."""
    out = subprocess.run(
        ["git", "status", "--porcelain", "--no-renames", "--relative", "."],
        cwd=SOFTWARE,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line[3:].strip('"') for line in out.splitlines()]


def selected(paths: list[str]) -> list[Suite]:
    def hit(suite: Suite, path: str) -> bool:
        tests = f"tests/{suite.name}/"
        return path.startswith(tests) or any(
            path == s or path.startswith(f"{s}/") for s in suite.sources
        )

    return [s for s in SUITES if any(hit(s, p) for p in paths)]


def run_python(suite: Suite, jobs: int, report: pathlib.Path, env: dict) -> tuple:
    xml = report.with_suffix(".xml")
    tests = SOFTWARE / "tests" / suite.name
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        str(tests),
        "-q",
        "-n",
        str(jobs),
        f"--junitxml={xml}",
        "--import-mode=prepend",
        f"--rootdir={tests}",
        "-p",
        "no:cacheprovider",
    ]
    code = subprocess.run(cmd, cwd=ROOT, check=False, env=env).returncode
    if code == 5:  # no test collected: the stage is not written yet
        return 0, []
    tree = ET.parse(xml).getroot().iter("testcase")
    return code, [
        (f"{c.get('classname')}.{c.get('name')}", float(c.get("time", 0))) for c in tree
    ]


def run_cargo(suite: Suite, jobs: int, env: dict) -> int:
    for package, *targets in suite.cargo:
        cmd = ["cargo", "test", "-q", "-p", package, "--profile", "stage"]
        cmd += ["-j", str(jobs)]
        for target in targets:
            cmd += ["--test", target]
        code = subprocess.run(cmd, cwd=SOFTWARE, check=False, env=env).returncode
        if code:
            return code
    return 0


def python_audit(shard: str) -> dict:
    """Shard `I/N` of the Python tiers' mutants, on a spec build: each must fail
    one of AUDITED, or stand in tests/known.txt."""
    index, count = map(int, shard.split("/"))
    listing = "from kohakutpu.stack import rs; print(*rs.mutants())"
    cmd = [sys.executable, "-c", listing]
    names = subprocess.run(
        cmd, capture_output=True, text=True, check=True
    ).stdout.split()
    names = [n for n in names if n.startswith(PYTHON_MUTANTS)][index::count]
    known = (SOFTWARE / "tests" / "known.txt").read_text(encoding="utf-8").split("\n")
    known = {line.split()[0] for line in known if line.strip()}

    t0 = time.monotonic()
    pythonpath = str(SOFTWARE / "tests") + os.pathsep + os.environ.get("PYTHONPATH", "")
    caught, survivors = {}, []
    for name in names:
        env = {**os.environ, "KTPU_MUTANT": name, "PYTHONPATH": pythonpath}
        for suite in AUDITED:
            tests = SOFTWARE / "tests" / suite
            cmd = [sys.executable, "-m", "pytest", str(tests), "-q", "-x", "-n", "0"]
            cmd += ["-p", "mutated", "-p", "no:cacheprovider", f"--rootdir={tests}"]
            cmd += ["--import-mode=prepend", "-k", "not lenet"]
            done = subprocess.run(
                cmd, cwd=ROOT, env=env, capture_output=True, check=False
            )
            if done.returncode:
                caught[name] = suite
                break
        else:
            survivors.append(name)
    print(f"{len(caught)} of {len(names)} Python mutants caught")
    for name in survivors:
        print(f"  {name}: Untold")
    stale = sorted(known.intersection(caught))
    unexpected = sorted(set(survivors) - known)
    return {
        "suite": f"python mutants {shard}",
        "seconds": round(time.monotonic() - t0, 2),
        "budget": 0.0,
        "tests": len(names),
        "slow": [],
        "code": 0,
        "caught": caught,
        "survivors": survivors,
        "passed": not stale and not unexpected,
    }


def emu_audit(jobs: int) -> dict:
    """ka-emu's mutants, on its own `mutants` build: each must fail firmware/emu,
    or stand in that suite's known.txt."""
    text = (SOFTWARE / FRAMEWORK / "ka-emu" / "src" / "mutant.rs").read_text("utf-8")
    names = re.findall(r'"(emu\.[^"]+)"', text)
    tests = SOFTWARE / "tests" / "firmware" / "emu"
    known = (tests / "known.txt").read_text(encoding="utf-8").split("\n")
    known = {line.split()[0] for line in known if line.strip()}

    target = ROOT / "build" / "emu-mutants"
    build = ["--profile", "stage", "--target-dir", str(target)]
    build += ["-p", "ka-emu", "--features", "mutants", "-j", str(jobs)]
    env = {**os.environ, **dict(EMU_ENV), "KA_EMU_MODEL": str(target / "model")}
    subprocess.run(
        ["cargo", "build", "--bin", "vsim", *build], cwd=SOFTWARE, env=env, check=True
    )
    (target / "model" / "obj_dir").mkdir(parents=True, exist_ok=True)
    vsim = target / "model" / "obj_dir" / "vsim"
    vsim.unlink(missing_ok=True)
    vsim.hardlink_to(target / "stage" / "vsim")

    t0 = time.monotonic()
    caught, survivors = {}, []
    for name in names:
        env["KA_EMU_MUTANT"] = name
        cargo = ["cargo", "test", "-q", "--test", "firmware_hart"]
        cargo += ["--test", "firmware_probe", "--test", "firmware_queue", *build]
        pytest = [
            sys.executable,
            "-m",
            "pytest",
            str(tests),
            "-q",
            "-x",
            "-n",
            str(jobs),
        ]
        pytest += ["-p", "no:cacheprovider", f"--rootdir={tests}"]
        for cmd, cwd in ((cargo, SOFTWARE), (pytest, ROOT)):
            done = subprocess.run(
                cmd, cwd=cwd, env=env, capture_output=True, check=False
            )
            if done.returncode:
                caught[name] = cmd[0]
                break
        else:
            survivors.append(name)
    print(f"{len(caught)} of {len(names)} emulator mutants caught")
    for name in survivors:
        print(f"  {name}: Untold")
    stale = sorted(known.intersection(caught))
    unexpected = sorted(set(survivors) - known)
    return {
        "suite": "emulator mutants",
        "seconds": round(time.monotonic() - t0, 2),
        "budget": 0.0,
        "tests": len(names),
        "slow": [],
        "code": 0,
        "caught": caught,
        "survivors": survivors,
        "passed": not stale and not unexpected,
    }


def run(suite: Suite, jobs: int, mutants: str | None) -> dict:
    REPORTS.mkdir(parents=True, exist_ok=True)
    report = REPORTS / (suite.name.replace("/", "-") + ".json")
    env = {**os.environ, **dict(suite.env), "RAYON_NUM_THREADS": str(jobs)}
    if mutants:
        env["KTPU_STAGE_MUTANTS"] = mutants
    t0 = time.monotonic()
    code, tests = 0, []
    if suite.cargo:
        code = run_cargo(suite, jobs, env)
    if suite.pytest and code == 0:
        code, tests = run_python(suite, jobs, report, env)
    seconds = time.monotonic() - t0
    slow = [(name, t) for name, t in tests if t > suite.per_test]
    budgeted = mutants or (seconds <= suite.budget and not slow)
    result = {
        "suite": suite.name,
        "seconds": round(seconds, 2),
        "budget": suite.budget,
        "tests": len(tests),
        "slow": slow,
        "code": code,
        "passed": code == 0 and budgeted,
    }
    report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=cast(str, __doc__).split("\n")[0])
    ap.add_argument("suites", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--mutants", nargs="?", const="0/1", metavar="I/N")
    ap.add_argument("--python-mutants", nargs="?", const="0/1", metavar="I/N")
    ap.add_argument("--emu-mutants", action="store_true")
    default_jobs = min(JOBS, os.cpu_count() or JOBS)
    ap.add_argument("--jobs", type=int, default=default_jobs)
    args = ap.parse_args()

    if args.list:
        for s in SUITES:
            print(f"{s.name:24} {s.budget:6.0f} s  {'cargo' if s.cargo else 'pytest'}")
        return 0
    if args.suites or args.python_mutants or args.emu_mutants:
        chosen = [s for s in SUITES if s.name in args.suites]
    elif args.all:
        chosen = [s for s in SUITES if s.default]
    else:
        chosen = [s for s in selected(changed()) if s.default]

    if args.python_mutants:
        results = [python_audit(args.python_mutants)]
    elif args.emu_mutants:
        results = [emu_audit(args.jobs)]
    else:
        results = [run(s, args.jobs, args.mutants) for s in chosen]
    for r in results:
        state = "ok  " if r["passed"] else "FAIL"
        print(f"{state} {r['suite']:24} {r['seconds']:7.1f} / {r['budget']:.0f} s")
        for name, t in r["slow"]:
            print(f"     over budget {t:.1f} s: {name}")
    (REPORTS / "last.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    total = sum(r["seconds"] for r in results)
    within = not (args.all and not args.mutants and total > ALL_BUDGET)
    if not within:
        print(f"FAIL every stage              {total:7.1f} / {ALL_BUDGET:.0f} s")
    return 0 if within and all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
