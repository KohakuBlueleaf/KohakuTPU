"""Tiered checks, so the inner loop is seconds and not minutes.

    python scripts/py/check.py fast     ~5 s    pure Python, no simulator
    python scripts/py/check.py unit     ~70 s   + the two RTL benches that
                                                catch most of what breaks
    python scripts/py/check.py full     ~6 min  every bench in xsim.py

Run `fast` after every edit, `unit` before believing anything works, and
`full` only before calling something done. Nearly every bug in this design so
far was catchable by `fast` or `unit`; reaching for `full` each time turns a
ten-second question into a five-minute one and teaches you to stop asking.

EVERY CHECK IS BOUNDED, and reports a hang AS a hang. A wedged bench does not
fail: it runs on until its own Verilog watchdog fires -- hundreds of
milliseconds of simulated time, minutes of wall clock at the speed xsim runs
this design -- and a bench whose internal wait expires may continue anyway and
grade whatever the untouched memory happened to hold. So a stall arrives as a
WRONG ANSWER after a long wait, which reads as a slow simulation and points at
the datapath, several modules away from the module that stopped. That is not
hypothetical: it is exactly how mx_mesh2x2_tb presented before it was deleted --
556 s, then every output element equal to its initial value. tests/mas/
mag_driver_tb.v makes the same argument and answers it from the inside, with a
progress watchdog on a monotonic sum of every retire counter and an `AWAIT
bound on every host handshake. This is the outer half of it, for the benches
that have no such thing.
"""

import argparse
import os
import pathlib
import signal
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
DRIVER = ROOT / "driver"
PY = DRIVER / ".venv" / "Scripts" / "python.exe"
if not PY.exists():  # posix layout
    PY = DRIVER / ".venv" / "bin" / "python"

# Seconds one check may take before it is declared STALLED and killed. Derived
# from what a healthy check costs rather than guessed: every bench in `full`
# runs in about 12 s, so this is ~25x the real figure and absorbs a loaded
# machine. Generous on purpose -- a check that reports STALLED when it was only
# slow makes the whole tier untrustworthy, which is the same damage a red tier
# does.
DEFAULT_TIMEOUT = 300
# Per-check overrides, by label. A check that legitimately outgrows the default
# belongs here with a reason, so the default keeps meaning "much longer than
# anything healthy" instead of drifting up to cover one slow outlier.
TIMEOUTS = {}

# (label, argv, cwd). Ordered cheapest first so a failure stops the run early.
FAST = [
    ("python tests", [str(PY), "-m", "pytest", "tests/", "-q"], DRIVER),
    ("ruff", [str(PY), "-m", "ruff", "check", "src", "tests"], DRIVER),
]
UNIT = FAST + [
    ("mx_quant vs model", [str(PY), "run_quant_check.py"], DRIVER),
    ("cluster_node", [str(PY), "scripts/py/xsim.py", "cluster_node"], ROOT),
    # Cheap, and it covers the one structure that has now broken twice under
    # concurrent clusters. Both times the only symptom at system level was a
    # GEMM that never finished.
    ("mag_wslot", [str(PY), "scripts/py/xsim.py", "mag_wslot"], ROOT),
    # The only tier-`unit` bench that instantiates mx_cluster_cu. Without it a
    # change to the CU -- the module most often edited -- is not compiled at
    # all until the driver bench runs, and a plain declaration-order error
    # survives a green `unit`.
    ("mag_system", [str(PY), "scripts/py/xsim.py", "mag_system"], ROOT),
]
FULL_BENCHES = ["fpacc", "acu", "cluster", "cluster_node", "system", "system32",
                "mag_wslot", "mag_system"]
FULL = FAST + [("mx_quant vs model", [str(PY), "run_quant_check.py"], DRIVER)] + [
    (b, [str(PY), "scripts/py/xsim.py", b], ROOT) for b in FULL_BENCHES
]

TIERS = {"fast": FAST, "unit": UNIT, "full": FULL}


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
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, check=False)
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    proc.kill()


def run_check(argv, cwd, timeout):
    """Run one check. Returns (ok, stalled, output)."""
    # start_new_session so the whole pipeline is one process group to signal;
    # stderr folded into stdout so the tail below reads in the order it
    # happened rather than as two separate streams.
    kw = {} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, **kw)
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
    ap.add_argument("--keep-going", action="store_true",
                    help="run everything even after a failure")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                    help="seconds per check before it is called STALLED "
                         f"(default {DEFAULT_TIMEOUT})")
    args = ap.parse_args()

    failures = []
    total = time.time()
    for label, argv, cwd in TIERS[args.tier]:
        limit = TIMEOUTS.get(label, args.timeout)
        t0 = time.time()
        ok, stalled, out = run_check(argv, cwd, limit)
        dt = time.time() - t0
        status = "ok" if ok else ("STALL" if stalled else "FAIL")
        print(f"  {status:<5} {label:<22} {dt:6.1f}s")
        if not ok:
            failures.append(f"{label} (STALLED)" if stalled else label)
            if stalled:
                # Said in full, because the whole point is that this is not the
                # same event as a FAIL: nothing was measured, so the last thing
                # printed below is where it stopped, not what was wrong.
                print(f"       STALLED -- no result in {limit} s, killed. "
                      "This is a hang, not a slow bench.")
            tail = [ln for ln in out.splitlines() if ln.strip()]
            for ln in tail[-15:]:
                print(f"       {ln}")
            if not args.keep_going:
                break

    print(f"  {'-' * 40}\n  {args.tier}: "
          f"{'PASS' if not failures else 'FAIL ' + ', '.join(failures)}"
          f"   {time.time() - total:.1f}s")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
