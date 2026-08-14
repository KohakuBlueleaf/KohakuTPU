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
"""

import argparse
import os
import pathlib
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
    # `scripts` is in scope because it is not scratch: check.py, xsim.py and
    # gen_mesh.py are all load-bearing and were unlinted.
    py("ruff", "-m", "ruff", "check", "compiler", "driver", "scripts"),
    py("black", "-m", "black", "--check", "-q", "compiler", "driver", "scripts"),
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
SNAP_DIRS = ("src", "tests", "scripts", "examples", "boards", "compiler", "driver")
SNAP_FILES = ("pyproject.toml", "setup.cfg", "ruff.toml", ".ruff.toml")


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
    args = ap.parse_args()

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

    ran, skipped = len(results), len(checks) - len(results)
    names = ", ".join(
        r["label"] + (" (STALLED)" if r["stalled"] else "") for r in failures
    )
    print(
        f"  {'-' * 40}\n  {args.tier}: "
        f"{'PASS' if not failures else 'FAIL ' + names}"
        f"   {ran}/{len(checks)} ran"
        + (f", {skipped} skipped after failure" if skipped else "")
        + f", -j{jobs}   {time.time() - total:.1f}s"
    )
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
