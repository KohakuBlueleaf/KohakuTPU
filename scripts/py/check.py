"""Tiered checks, run in parallel, so the inner loop is seconds and not minutes.

    fast     11 s   DSL and autoschedule against the L1 simulator, pure Python
    unit     40 s   + the RTL benches that have caught the most
    blocks   63 s   every block's own bench, one fault per module
    e2e      40 s   L1 -> machine code -> xsim, planner and DSL both
    full            all of the above

Three separable questions, so three tiers: does the compiler mean the right
thing (`fast`), does each block do what it says (`blocks`), and do the two
agree on real hardware (`e2e`). Run `fast` after every edit and `full` only
before calling something done. Times are measured at -j4, not estimated.

EVERY CHECK IS BOUNDED, AND REPORTS A HANG AS A HANG. A wedged bench does not
fail -- it runs to its own Verilog watchdog and then grades whatever the
untouched memory held, so a stall arrives as a wrong answer after a long wait
and points at the datapath rather than at the module that stopped. That is how
mx_mesh2x2_tb presented before it was deleted: 556 s, then every output element
equal to its initial value.

WHAT MAY RUN BESIDE WHAT IS A CORRECTNESS QUESTION, NOT A TUNING ONE -- see
LANES below. Parallelism here is not "run everything at once and hope".
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
PY = sys.executable

# ~25x what a healthy check costs. A STALLED verdict on a merely slow check
# would discredit the tier, and parallelism is what spends the margin.
DEFAULT_TIMEOUT = 300
# Per-check overrides by label, so the default keeps meaning "longer than
# anything healthy" rather than drifting up to cover one outlier.
TIMEOUTS = {}

# ------------------------------------------------------------------- lanes
# A lane is a resource two checks would FIGHT OVER, not a category.
#
#   session   Session compiles into ONE build/sim_session and holds it with a
#             lock (sim.py s171); a second does not race, it fails outright.
#   xsim      one build/xsim_<bench> per bench, so distinct benches are
#             independent -- the same bench twice is not.
#   py        no simulator, no shared state.
EXCLUSIVE = {"session"}
_LANE_LOCKS = {}
_LANE_LOCKS_GUARD = threading.Lock()


def lane_lock(lane):
    """The lock for an exclusive lane, created once and shared."""
    with _LANE_LOCKS_GUARD:
        return _LANE_LOCKS.setdefault(lane, threading.Lock())


def check(label, argv, lane="py", cwd=ROOT):
    return {"label": label, "argv": argv, "cwd": cwd, "lane": lane}


def py(label, *args):
    return check(label, [str(PY), *args], lane="py")


# A build root of our own, so a check never wipes the directory out from under
# a bench someone is running by hand at the same time.
#
# PER-INVOCATION: `--snapshot` isolates the sources and this path did not, so
# two concurrent checks shared one xsim build directory and destroyed each
# other's. It reads as a dozen benches failing at once with tool errors
# ("Unable to open command file xvlog.f"), never an RTL message.
CHECK_BUILD = pathlib.Path(
    os.environ.get("KOHAKU_CHECK_BUILD") or (ROOT / "build" / f"check-{os.getpid()}")
)


def bench(name, *extra):
    argv = [str(PY), "scripts/py/xsim.py", name, "--build-root", str(CHECK_BUILD)]
    return check(name, argv + list(extra), lane="xsim")


# ------------------------------------------------------------------- tiers
# Split by directory: the three parts share nothing, so it is free parallelism
# and the label names which layer broke.
FAST = [
    py("pytest unit", "-m", "pytest", "tests/ktpu/unit", "-q"),
    py("pytest hw", "-m", "pytest", "tests/ktpu/hw", "-q"),
    py("pytest integration", "-m", "pytest", "tests/ktpu/integration", "-q"),
    # `scripts` is in scope because it is not scratch: check.py, xsim.py,
    # gen_mesh.py and isa_study.py are all load-bearing and were unlinted.
    py("ruff", "-m", "ruff", "check", "src/ktpu", "tests", "examples", "scripts"),
    py("black", "-m", "black", "--check", "-q", "src/ktpu", "tests"),
]

# The cheap benches that have historically caught the most.
UNIT = FAST + [
    py("mx_quant vs model", "scripts/py/run_quant_check.py"),
    bench("cluster_node"),
    # Cheap, and it covers the one structure that has now broken twice under
    # concurrent clusters. Both times the only symptom at system level was a
    # GEMM that never finished.
    bench("mag_wslot"),
    # The only tier-`unit` bench that instantiates mx_cluster_cu. Without it a
    # change to the CU -- the module most often edited -- is not compiled at
    # all until the driver bench runs, and a plain declaration-order error
    # survives a green `unit`.
    bench("mag_system"),
]


def all_benches():
    """Every bench xsim.py defines, read FROM xsim.py.

    Listing them here as well would let the two drift, and the failure mode of
    that drift is silent: a new block gets a bench, nobody adds it to a tier,
    and `full` stays green while covering less than it did.
    """
    sys.path.insert(0, str(ROOT / "scripts" / "py"))
    import xsim

    return sorted(xsim.BENCHES)


# Each block's own simulation, which is the level a fault is cheapest to read
# at: a failure names the module instead of naming "the machine".
BLOCKS = [bench(b) for b in all_benches()]

# THE WHOLE COMPILER, on shapes small enough to run every time. Both drive the
# same RTL through the same Session; the only difference is who emitted the
# instructions, so running both separates a compiler fault from a hardware one.
E2E = [
    check(
        "planner -> xsim",
        [str(PY), "scripts/py/run_matmul.py", "--quiet"],
        lane="session",
    ),
    check(
        "DSL -> L3 -> xsim",
        [str(PY), "scripts/py/run_dsl.py", "--m", "64", "--k", "64", "--n", "64"],
        lane="session",
    ),
]

TIERS = {
    "fast": FAST,
    "unit": UNIT,
    "blocks": FAST + BLOCKS,
    "e2e": FAST + E2E,
    "full": FAST
    + [py("mx_quant vs model", "scripts/py/run_quant_check.py")]
    + BLOCKS
    + E2E,
}


def kill_tree(proc):
    """Kill `proc` AND its children.

    The thing that actually hangs is xsim, two levels down: check.py runs
    xsim.py, which runs xsim.bat, which runs the simulator. Killing only the
    direct child leaves the simulator running against the same build directory
    the next attempt is about to delete -- so the timeout would trade one wedged
    run for a corrupted one, plus a licence nobody released.
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


# What a check needs to run: sources, benches, the scripts themselves, and the
# config ruff and black read. Build directories are NOT copied -- they are
# regenerated, and they are the bulk of the tree.
SNAP_DIRS = ("src", "tests", "scripts", "examples", "boards")
SNAP_FILES = ("pyproject.toml", "setup.cfg", "ruff.toml", ".ruff.toml")


def make_snapshot(dest):
    """Copy the sources into `dest` and return it.

    A measurement of a tree that is being edited underneath it is not a
    measurement. This has already cost two whole-machine runs -- one killed by
    `mx_acu_fp.v` mid-edit, one by `vec_lanes.v` -- both of which looked like
    regressions and were not. Copying first makes a run reproducible and
    immune to whatever lands next.
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
    # start_new_session so the whole pipeline is one process group to signal;
    # stderr folded into stdout so the tail below reads in the order it
    # happened rather than as two separate streams.
    kw = {} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        # A tool that emits a non-cp950 byte would otherwise raise inside the
        # reader thread, leaving `out` None and crashing the summary instead of
        # reporting the failure that produced it.
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
    env = None
    if args.snapshot:
        snap = make_snapshot(args.snapshot)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(snap / "src")
        checks = [dict(c, cwd=snap) for c in checks]
        print(f"  snapshot {snap}")
    jobs = max(1, args.jobs)
    # WITH -j1 A FAILURE STILL STOPS THE RUN, because that is the mode someone
    # uses when they are bisecting and every later check is noise. In parallel
    # the work is already in flight, so stopping early would only hide results
    # that were paid for.
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
            # Said in full, because the whole point is that this is not the
            # same event as a FAIL: nothing was measured, so the last thing
            # printed below is where it stopped, not what was wrong.
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
