"""Run one tier of checks in parallel, and report a hang as a hang.

    python scripts/py/check.py [fast|unit|blocks|e2e|full] [-j N]

    fast     11 s   DSL and autoschedule against the L1 simulator, pure Python
    unit     40 s   + the RTL benches that have caught the most
    blocks   63 s   every block's own bench, one fault per module
    e2e      40 s   L1 -> machine code -> xsim, planner and DSL both
    full            all of the above

Times are measured at -j4. Every check is bounded: one that produces no result
inside its budget is killed and reported as STALLED, which is a different event
from a FAIL and is printed as one. Exits non-zero if any check failed.

`--counts LEDGER` records the numbers every check printed on its PASS/FAIL
lines, and `--counts-baseline LEDGER` fails the run when any of them moved. A
reformat is supposed to change no behaviour, and a green suite does not say
that -- 503 checks becoming 501 is still a PASS.
"""

import argparse
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(__file__).resolve().parents[2]
# The repo venv, NOT whatever launched this: it is the only interpreter with
# tinygrad and both namespace halves installed.
_VENV = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
PY = str(_VENV) if _VENV.exists() else sys.executable

# ~25x what a healthy check costs at -j4.
DEFAULT_TIMEOUT = 300
# Per-check overrides by label, for a check that is healthy and slow.
TIMEOUTS = {}

# ------------------------------------------------------------------- lanes
EXCLUSIVE = {"session"}
_LANE_LOCKS = {}
_LANE_LOCKS_GUARD = threading.Lock()


def lane_lock(lane):
    """The lock for an exclusive lane, created once and shared.

    A lane is a resource two checks would fight over: `session` is the single
    `build/sim_session` a Session holds a lock on, `xsim` a build directory per
    bench, `py` nothing at all. Only the lanes in `EXCLUSIVE` are serialised.
    """
    with _LANE_LOCKS_GUARD:
        return _LANE_LOCKS.setdefault(lane, threading.Lock())


def check(label, argv, lane="py", cwd=ROOT):
    return {"label": label, "argv": argv, "cwd": cwd, "lane": lane}


def py(label, *args):
    return check(label, [str(PY), *args], lane="py")


# Per pid: two concurrent checks sharing one xsim build directory destroy each
# other's, and it reads as a dozen tool errors at once, never an RTL message.
CHECK_BUILD = pathlib.Path(
    os.environ.get("KOHAKU_CHECK_BUILD") or (ROOT / "build" / f"check-{os.getpid()}")
)


def bench(name, *extra, label=None, root=None):
    argv = [
        str(PY),
        "scripts/py/xsim.py",
        name,
        "--build-root",
        str(root or CHECK_BUILD),
    ]
    return check(label or name, argv + list(extra), lane="xsim")


def bench_var(name, *extra):
    """The same bench under extra defines, in a build root of its own.

    `xsim` names the build directory after the bench alone, so a variant left in
    the shared root races the plain run at -j4 and reads as a tool error.
    """
    label = " ".join([name, *extra])
    return bench(name, *extra, label=label, root=CHECK_BUILD / label.replace(" ", "_"))


# ------------------------------------------------------------------- tiers
FAST = [
    py("pytest compiler", "-m", "pytest", "compiler/tests", "-q"),
    # FROM `driver/`: at the repo root `examples` resolves to the top-level
    # examples/, and driver/examples/saxpy is then invisible.
    check(
        "pytest driver",
        [PY, "-m", "pytest", "tests", "-q"],
        cwd=ROOT / "driver",
    ),
    # A demo nobody runs is a claim nobody checks, and this one is 28 DiT blocks
    # end to end. Not in `testpaths`, so it is named here.
    py("pytest anima", "-m", "pytest", "demos/kohakutpu/anima/tests", "-q"),
    # EVERY directory, for the reason `vstyle` covers every .v file: a gate over
    # a subset reads as done, and tests/pe/tools is 34 files the suites grade by.
    py("ruff", "-m", "ruff", "check", "."),
    py("black", "-m", "black", "--check", "-q", "."),
    # A doc naming a moved file is drift a rename produces by the dozen and
    # nothing else measures: 103 of these had accumulated behind two renames.
    py("doc paths", "scripts/py/docpaths.py"),
    # The normative parameter tables against the RTL they describe. 42
    # mismatches had accumulated, including `ADDR_W` documented as 34 in five
    # tables when every module on the memory path declares 40.
    py("spec params", "scripts/py/specparams.py"),
    # A cited LINE rots faster than a path: four had drifted off, one by 28.
    py("doc lines", "scripts/py/doclines.py"),
    # `docpaths.py` strips the fragment, so a link into a RENAMED heading
    # resolves and lands the reader at the top of the page. 9 had.
    py("doc anchors", "scripts/py/docanchors.py"),
    # The framework may not instantiate a project's module. Every build list
    # carries both trees, so nothing else notices when one does.
    py("deps", "scripts/py/deps.py"),
    # Nothing BUILDS from FILES.f, so nothing noticed 7 of 12 drifting -- one
    # had dropped its own library top.
    py("files.f", "scripts/py/filesf.py", "--check"),
    # Unwired, the SIMT one pointed at a directory deleted when the PE moved
    # and had been reporting DRIFT to nobody.
    py("simd isa", "tests/pe/tools/rv_simd_emit.py", "--check"),
    py("simt isa", "tests/pe/tools/rv_simt_emit.py", "--check"),
    # The style rules, over EVERY .v file. The count was reported for the live
    # source alone for months while tests/ held more findings than the whole
    # tree it claimed to describe.
    py("vstyle", "scripts/py/vstyle.py", "src", "tests"),
]

# The cheap benches that have historically caught the most.
UNIT = FAST + [
    # mx_quant.v's only cross-check against the spec: the bench itself computes
    # nothing, it only dumps what the circuit produced.
    py("mx_quant vs model", "scripts/py/run_quant_check.py"),
    bench("cluster_node"),
    # Broken twice under concurrent clusters, both times presenting at system
    # level only as a GEMM that never finished.
    bench("mag_wslot"),
    # The only bench in this tier that instantiates mx_cluster_cu, so without
    # it a declaration-order error in the CU survives a green `unit`.
    bench("mag_system"),
    # Cheap, and it is the only cover the L2 adapter has: the module shipped
    # with two OOC runs and no bench at all for months.
    bench("l2_adapter"),
]


def all_benches():
    """Every bench name `xsim.py` defines, sorted. Read from `xsim.BENCHES`."""
    sys.path.insert(0, str(ROOT / "scripts" / "py"))
    import xsim

    return sorted(xsim.BENCHES)


BLOCKS = [bench(b) for b in all_benches()] + [
    # NOT covered by any bench above. mm_xform and mag_system both compare the
    # occupant against ANOTHER INSTANCE of itself, so an arithmetic change
    # cancels out and reads as a pass; this is the only cross-check against the
    # software model.
    py("mx_quant vs model", "scripts/py/run_quant_check.py"),
    # `all_benches()` runs every name with NO defines, so the pumped path -- the
    # one that ships -- had no cover here at all until these three.
    bench_var("cluster_data", "-d", "MX_CU_PUMP"),
    bench_var("cluster_node_pump", "-d", "MX_L1OFF"),
    bench_var("cluster_node_pump", "-d", "MX_L1WRAP"),
    # The L2 store on MAG's converged path, where the mover and the interlink
    # can reach it. Same checks as the default placement, so it is an A/B.
    bench_var("mm_mesh_stage", "-d", "MM_L2_PORT"),
    bench_var("interlink_stage", "-d", "MM_L2_PORT"),
    # The MOVER as a requester of the store: DRAM -> aperture -> DRAM.
    bench_var("mover_l2", "-d", "MV_L2"),
    # Four meshes with a store in each: forwarding and staging together.
    bench_var("mover_l2_chain", "-d", "MV_L2"),
    # MAG behind noc_local_cdc. The default build takes the direct branch, and
    # an unelaborated generate branch is not checked at all.
    bench_var("interlink_2mesh_1m", "-d", "MM_MAG_CDC"),
]

# Compiler-emitted instructions through the real RTL, the one tier that separates
# a compiler fault from a hardware one. Empty since `src/ktpu` was retired.
E2E = []

TIERS = {
    "fast": FAST,
    "unit": UNIT,
    "blocks": FAST + BLOCKS,
    "e2e": FAST + E2E,
    "full": FAST + BLOCKS + E2E,
}


def kill_tree(proc):
    """Kill `proc` and every process it spawned.

    What hangs is xsim, two levels down. An orphaned simulator holds the build
    directory the next attempt deletes, and the licence with it.
    """
    if os.name == "nt":
        # No process groups worth the name on Windows; taskkill /T walks the
        # child list the kernel already keeps.
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    proc.kill()


# Sources, benches, scripts and the config ruff and black read. Build
# directories are regenerated and are the bulk of the tree, so they are left.
#
# THE DOCS ARE IN IT because a check that reads them resolves its paths from
# its own location, so under --snapshot it looked in a tree with no docs/ at
# all and passed on zero files. A gate that cannot fail is not a gate.
SNAP_DIRS = (
    "src",
    "tests",
    "scripts",
    "examples",
    "boards",
    "compiler",
    "driver",
    # Linted like everything else, so it has to be in the copy the gate reads.
    "demos",
    "docs",
    "docs-web/src",
    # README.md links to both; without them the doc check reports two dangling
    # references that exist in the tree it is supposed to be a copy of.
    "image",
)
SNAP_FILES = (
    "pyproject.toml",
    "setup.cfg",
    "ruff.toml",
    ".ruff.toml",
    "README.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    "LICENSE",
)


def make_snapshot(dest):
    """Copy the sources into `dest` and return it, replacing what was there.

    A tree edited underneath a run reports the edit as a regression; two whole
    machine runs were lost that way.
    """
    dest = pathlib.Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for d in SNAP_DIRS:
        if (ROOT / d).is_dir():
            shutil.copytree(ROOT / d, dest / d)
    for f in SNAP_FILES:
        if (ROOT / f).is_file():
            shutil.copy2(ROOT / f, dest / f)
    return dest


# ------------------------------------------------------------------- counts
# CONTRIBUTING's bar for a reformat is the numbers, not the PASS. This collects
# them for a whole tier so a tree-wide pass can be compared rather than trusted.
VERDICT = re.compile(r"\b(?:PASS|FAIL)\b")
INTS = re.compile(r"\d+")


def verdict_numbers(out):
    """Every integer on the check's own PASS/FAIL lines, in order.

    Deliberately shape-blind: the tree writes `PASS -- 503 checks, 0 errors`,
    `PASS mm_mover_tb: 16 checks` and `PASS -- 4000 vectors, 0 mismatches`, and
    a parser per shape would go stale the first time a bench is reworded. A
    label whose output has no verdict line (ruff, black, pytest) yields [].
    """
    return [
        int(v)
        for ln in out.splitlines()
        if VERDICT.search(ln)
        for v in INTS.findall(ln)
    ]


def report_counts(results, baseline):
    """Compare this run's numbers against `baseline`. Returns the drifted rows.

    A label missing from either side is NOT drift: the bench list changes, and
    calling that a regression would make the ledger something people delete.
    """
    drift = []
    for r in results:
        was = baseline.get(r["label"])
        now = r["counts"]
        if was is None or was == now:
            continue
        drift.append((r["label"], was, now))
    return drift


def run_check(argv, cwd, timeout, env=None):
    """Run one check. Returns (ok, stalled, output)."""
    # One process group, so a timeout can signal the whole pipeline at once.
    kw = {} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        # A non-cp950 byte from a tool otherwise raises in the reader thread and
        # crashes the summary instead of reporting the failure that produced it.
        errors="replace",
        **kw,
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode == 0, False, out
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        out, _ = proc.communicate()
        return False, True, out or ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tier", choices=sorted(TIERS), nargs="?", default="fast")
    ap.add_argument(
        "--keep-going", action="store_true", help="run everything even after a failure"
    )
    ap.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=4,
        help="checks to run at once (default 4; 1 restores sequential fail-fast)",
    )
    ap.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help="seconds per check before it is called STALLED "
        f"(default {DEFAULT_TIMEOUT})",
    )
    ap.add_argument(
        "--snapshot",
        nargs="?",
        const=str(ROOT / "build" / "snapshot"),
        default=None,
        help="copy the sources first and check THAT, so a concurrent edit "
        "cannot turn a run into a false regression",
    )
    ap.add_argument(
        "--counts",
        help="write each check's PASS/FAIL numbers to this JSON ledger",
    )
    ap.add_argument(
        "--counts-baseline",
        help="compare against a ledger written earlier; drift fails the run",
    )
    args = ap.parse_args()

    baseline = {}
    if args.counts_baseline:
        baseline = json.loads(
            pathlib.Path(args.counts_baseline).read_text(encoding="utf-8")
        )

    checks = TIERS[args.tier]
    root = ROOT
    if args.snapshot:
        root = make_snapshot(args.snapshot)
        # Remapped, not replaced: a check with its own cwd keeps it.
        checks = [
            dict(c, cwd=root / pathlib.Path(c["cwd"]).relative_to(ROOT)) for c in checks
        ]
        print(f"  snapshot {root}")
    # `kohakutpu` is a namespace package split across both, so neither half
    # imports without the other on the path.
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(root / "compiler"), str(root / "driver")])
    jobs = max(1, args.jobs)
    # Only at -j1: in parallel the later checks are already in flight, so
    # stopping early would hide results that were paid for anyway.
    fail_fast = jobs == 1 and not args.keep_going

    stop = threading.Event()
    results = []
    results_guard = threading.Lock()
    total = time.time()

    def one(c):
        if stop.is_set():
            return None
        limit = TIMEOUTS.get(c["label"], args.timeout)
        lock = lane_lock(c["lane"]) if c["lane"] in EXCLUSIVE else None
        if lock:
            lock.acquire()
        t0 = time.time()
        try:
            ok, stalled, out = run_check(c["argv"], c["cwd"], limit, env)
        finally:
            if lock:
                lock.release()
        r = {
            "label": c["label"],
            "ok": ok,
            "stalled": stalled,
            "out": out,
            "counts": verdict_numbers(out),
            "dt": time.time() - t0,
            "limit": limit,
        }
        with results_guard:
            results.append(r)
            status = "ok" if ok else ("STALL" if stalled else "FAIL")
            print(f"  {status:<5} {r['label']:<22} {r['dt']:6.1f}s", flush=True)
        if not ok and fail_fast:
            stop.set()
        return r

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        list(pool.map(one, checks))

    failures = [r for r in results if not r["ok"]]
    for r in failures:
        print(f"\n  --- {r['label']} ---")
        if r["stalled"]:
            # Nothing was measured, so the lines below say where it stopped
            # rather than what was wrong.
            print(
                f"       STALLED -- no result in {r['limit']} s, killed. "
                "This is a hang, not a slow bench."
            )
        for ln in [ln for ln in r["out"].splitlines() if ln.strip()][-15:]:
            print(f"       {ln}")

    drift = report_counts(results, baseline) if baseline else []
    for label, was, now in drift:
        print(
            f"\n  --- {label}: COUNTS CHANGED ---\n       was {was}\n       now {now}"
        )

    if args.counts:
        led = pathlib.Path(args.counts)
        led.parent.mkdir(parents=True, exist_ok=True)
        led.write_text(
            json.dumps({r["label"]: r["counts"] for r in results}, indent=1) + "\n",
            encoding="utf-8",
        )
        print(f"  counts -> {led}")

    ran, skipped = len(results), len(checks) - len(results)
    names = ", ".join(
        r["label"] + (" (STALLED)" if r["stalled"] else "") for r in failures
    )
    if drift:
        names = ", ".join(filter(None, [names, f"{len(drift)} count(s) changed"]))
    print(
        f"  {'-' * 40}\n  {args.tier}: "
        f"{'PASS' if not (failures or drift) else 'FAIL ' + names}"
        f"   {ran}/{len(checks)} ran"
        + (f", {skipped} skipped after failure" if skipped else "")
        + (f", {len(baseline)} compared" if baseline else "")
        + f", -j{jobs}   {time.time() - total:.1f}s"
    )
    sys.exit(1 if (failures or drift) else 0)


if __name__ == "__main__":
    main()
