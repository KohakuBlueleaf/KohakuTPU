"""A live xsim session: elaborate once, then run problem after problem.

    with Session() as s:
        r = s.run(16, 32, 16)      # M, K, N

WHY THIS EXISTS. Compiling and elaborating the design costs ~10 s; the
simulation itself costs under one. Re-running `xvlog`/`xelab` per experiment
therefore spends 95% of its time rebuilding something that did not change.

Nothing about the problem is compiled in -- the bench reads its operand and
program files at time 0 -- so one snapshot serves any shape
:func:`ktpu.hw.bench.check` accepts. `restart` re-executes the initial
blocks, which re-reads those files, so a new problem costs only the simulation:

    cold  xvlog + xelab + xsim   ~15.6 s
    warm  restart ; run -all      ~0.8 s

That is the difference between batching experiments and exploring them, and it
is the only reason the visualiser can feel interactive.

The bench must be built with `-d SERVER`, which turns its `$finish` into
`$stop` -- `$finish` would take the process down and forfeit the snapshot.
"""

import contextlib
import math
import os
import pathlib
import queue
import re
import subprocess
import sys
import threading
import time

import numpy as np

from ktpu.hw import bench, disasm, formats, mxfp7, tensor

DEFAULT_VIVADO = pathlib.Path(r"D:\Xilinx\Vivado\2024.2\bin")
SENTINEL = "__KOHAKU_RDY__"

# ---- how long one `run -all` is allowed to take -------------------------
#
# A BOUND, NOT A PREDICTION, and derived from the problem rather than fixed.
# One constant cannot cover both ends of the range this session runs: the
# default 16x16x32 finishes in about a second and the eight-cluster reference
# -- 512x1024x256, ~121k simulated cycles on a 4x4 mesh -- does not finish in
# 180, which is what the old constant allowed. It reported "the design is most
# likely stalled" on a run that was healthy, and a guard that cries wolf is
# worse than no guard: it teaches you to raise the limit without reading it.
#
# WHAT THE BUDGET IS FOR. It is not the stall detector. The bench has a real
# one -- `stall` counts cycles since a monotonic sum of every retire counter
# last moved, and STALL_LIMIT cycles of that prints ROUND-FAIL and ends the
# simulation -- plus a whole-run WATCHDOG. A design that stops making progress
# therefore terminates ITSELF and hands the prompt back. What is left for this
# budget is the case those cannot see: xsim itself wedged, or the pipe died.
# So it can afford to be generous, and the message must not claim to know
# which of the two happened.
#
# Simulation cost is proportional to (simulated cycles) x (gates evaluated per
# cycle), and the cluster array dominates the gate count -- hence cluster-
# cycles. The coefficient is anchored on the one point measured this session:
# the eight-cluster reference completes inside 480 s, and the estimate below
# puts it near 950, so the budget carries about 2x headroom over a known-good
# run rather than sitting on top of it.
SIM_BASE_SECONDS = 60.0
SIM_SECONDS_PER_MCC = 600.0  # per million estimated cluster-cycles
# Cycles the bench spends per unit of work, each rounded UP from a measured
# run so the estimate is an over-estimate by construction.
UPLOAD_CYCLES_PER_WORD = 4  # measured ~3.2 on the eight-cluster reference
HOST_CYCLES_PER_WRITE = 8  # one AXI write through the crossbar
RUN_CYCLE_SLACK = 2.0  # against the peak-rate floor; measured is ~1.3x


class SimError(RuntimeError):
    pass


def _estimate_cycles(prob):
    """Simulated cycles a HEALTHY run of this problem should not exceed.

    Three terms, because the bench measures three and they scale differently:
    the operand upload grows with the tensors, the run with the arithmetic, and
    the host control traffic with the number of passes. Every coefficient is
    rounded up from a measured run, so this is an upper estimate rather than a
    best guess -- it feeds a timeout, where being low is a false alarm and
    being high costs only a longer wait on a genuinely wedged simulator.
    """
    upload = _host_words(prob) * UPLOAD_CYCLES_PER_WORD
    run = RUN_CYCLE_SLACK * prob.m * prob.k * prob.n / (MACS_PER_CLUSTER * prob.ncl)
    writes = sum(len(p.setup) + 4 * len(p.cmds) for p in prob.programs)
    return int(upload + run + writes * HOST_CYCLES_PER_WRITE)


def _timeout_message(budget, quiet, out):
    """Why the budget ran out, WITHOUT guessing which cause it was.

    Two distinguishable things, and the old message asserted the one it could
    not know. `quiet` is seconds since the simulator last printed anything:
    the bench announces its upload and its round count while it works, so
    recent output means it is running and simply slower than the budget, while
    silence from the start means nothing has been produced at all.

    Neither reading is "the design is stalled". The bench detects that itself
    -- STALL_LIMIT cycles without any retire counter moving prints ROUND-FAIL
    and ends the run -- so a stalled design comes back through the normal path
    with a diagnosis attached, not through here.
    """
    progressed = f"; the last line arrived {quiet:.0f}s ago, so it was still working"
    silent = "; nothing has been printed at all, so it may never have started"
    return (
        f"the simulation did not finish within its {budget:.0f}s budget"
        + (silent if quiet >= budget - 1 else progressed)
        + ".\nThe bench detects a stalled DESIGN itself -- it ends the run and "
        "reports ROUND-FAIL -- so reaching this instead points at a wedged "
        "simulator or a budget too small for the problem. Raise it with "
        "KOHAKU_SIM_TIMEOUT=<seconds>.\nlast output:\n  " + "\n  ".join(out[-20:])
    )


def _host_words(prob):
    """Words of the FP16 host image, which is what the upload pushes."""
    nk = prob.k // tensor.KBLOCK
    return (
        (prob.m // tensor.LANES + prob.n // tensor.LANES)
        * nk
        * (tensor.FP16_ENTRY_BYTES // 32)
    )


def _pid_of(owner):
    """The pid out of a 'pid 1234' lock file, or None if it is not one."""
    parts = owner.split()
    if len(parts) == 2 and parts[0] == "pid" and parts[1].isdigit():
        return int(parts[1])
    return None


def _pid_alive(pid):
    """Whether that process exists. Unknown counts as alive -- a lock is only
    reclaimed on proof the owner is gone, never on a guess."""
    if pid is None:
        return True
    if sys.platform == "win32":
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in r.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by someone else
        return True
    return True


class Session:
    """One elaborated snapshot plus the live xsim process running it."""

    def __init__(self, root=None, vivado=None, work=None):
        self.root = pathlib.Path(root or pathlib.Path(__file__).resolve().parents[3])
        self.vivado = pathlib.Path(
            vivado or os.environ.get("VIVADO_BIN", DEFAULT_VIVADO)
        )
        # ONE shared directory, held by a lock. Two sessions in the same
        # directory overwrite each other's snapshot and hex files mid-run and
        # the damage surfaces as a wrong answer, so they must be excluded --
        # but excluding them with a directory per process also throws away the
        # snapshot, and then every invocation pays the cold build above to
        # rebuild RTL which did not change. The lock gives both.
        self.work = pathlib.Path(work or self.root / "build" / "sim_session")
        self._lock = None
        self.proc = None
        self.started = False  # has `run -all` been issued at least once
        self.lock = threading.Lock()
        # The override wins outright when it is set, and it is remembered as an
        # override so `_budget` does not quietly scale past a number someone
        # chose on purpose.
        env = os.environ.get("KOHAKU_SIM_TIMEOUT")
        self.timeout_fixed = env is not None
        # Control commands -- `puts ready`, `restart` -- are not the run, so
        # they keep a short flat limit whatever the problem is.
        self.timeout = float(env) if env else 180.0
        self._q = queue.Queue()

    def _budget(self, prob):
        """Wall-clock seconds to allow this problem's `run -all`."""
        if self.timeout_fixed:
            return self.timeout
        cc = _estimate_cycles(prob) * prob.ncl
        return SIM_BASE_SECONDS + cc / 1e6 * SIM_SECONDS_PER_MCC

    # ---- lifecycle -----------------------------------------------------
    def _tool(self, name):
        # Windows will not resolve a .bat through CreateProcess, so name it in
        # full rather than relying on PATH.
        return str(self.vivado / name)

    def _env(self):
        env = {**os.environ}
        env["PATH"] = str(self.vivado) + ";" + env["PATH"]
        return env

    def _sh(self, cmd):
        r = subprocess.run(
            [self._tool(cmd[0])] + cmd[1:],
            cwd=self.work,
            env=self._env(),
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode:
            raise SimError(f"{cmd[0]} failed:\n{r.stdout[-4000:]}{r.stderr[-2000:]}")
        return r.stdout

    def _opts(self):
        """Compile options that are part of the snapshot's identity.

        `NCL` is compiled in -- it sizes the mesh and the cluster array -- so a
        snapshot built for two clusters cannot serve eight. It belongs here
        rather than in the mtime check, because nothing on disk changes when
        the environment does.
        """
        return ["-d SERVER", f"-d NCL={len(bench.CLUSTERS)}"]

    def snapshot_is_current(self):
        """True if the built snapshot covers these sources AND these options."""
        stamp = self.work / "xsim.dir" / "tb" / "xsimk.exe"
        opts = self.work / "xvlog.f"
        if not stamp.exists() or not opts.exists():
            return False
        want = self._opts()
        if opts.read_text().splitlines()[: len(want)] != want:
            return False
        newest = max((self.root / p).stat().st_mtime for p in bench.RTL)
        return stamp.stat().st_mtime >= newest

    def build(self, force=False):
        """Compile and elaborate. Skipped when the snapshot is already current."""
        self.work.mkdir(parents=True, exist_ok=True)
        if not force and self.snapshot_is_current():
            return False
        srcs = [str(self.root / p) for p in bench.RTL]
        # Options go through a command file: xvlog.bat is a batch script and it
        # splits `-d NAME=VALUE` at the `=`, so the value arrives as a filename.
        (self.work / "xvlog.f").write_text("\n".join(self._opts() + srcs) + "\n")
        self._sh(["xvlog.bat", "-sv", "-work", "w", "-f", "xvlog.f"])
        self._sh(
            [
                "xelab.bat",
                "-debug",
                "typical",
                "-timescale",
                "1ns/1ps",
                "-L",
                "xpm",
                "w.mag_driver_tb",
                "-s",
                "tb",
            ]
        )
        return True

    def _acquire(self):
        """Claim the work directory, or say who has it.

        Exclusive-create is the whole mechanism: it is atomic on every
        filesystem that matters, so two processes cannot both believe they own
        the snapshot.

        A lock whose owner is GONE is reclaimed, but only after checking that
        the pid is actually dead. Never reclaiming means one killed run leaves
        the directory unusable until someone deletes a file by hand; reclaiming
        on a timeout instead would eventually put two simulators in one
        directory, which is the failure this lock exists to prevent. Asking the
        OS whether the pid exists is the only test that is both.
        """
        if self._lock is not None:
            return
        self.work.mkdir(parents=True, exist_ok=True)
        path = self.work / "session.lock"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            owner = path.read_text().strip() if path.exists() else "unknown"
            if _pid_alive(_pid_of(owner)):
                raise SimError(
                    f"{self.work} is in use by {owner}. Close that session, or "
                    f"delete {path} if it is stale."
                ) from None
            path.unlink(missing_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"pid {os.getpid()}".encode())
        os.close(fd)
        self._lock = path

    def _release(self):
        if self._lock is not None:
            try:
                self._lock.unlink()
            except OSError:
                pass
            self._lock = None

    def start(self):
        if self.proc is not None:
            return
        self._acquire()
        self.build()
        self.proc = subprocess.Popen(
            [self._tool("xsim.bat"), "tb"],
            cwd=self.work,
            env=self._env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.started = False
        self._q = queue.Queue()
        threading.Thread(target=self._reader, daemon=True).start()
        self._tcl("puts ready", timeout=120)

    def _kill_tree(self):
        """Kill xsim AND the xsimk it spawned.

        proc.kill() reaches only the launcher. The simulator itself is a
        grandchild, and orphaning it is worse than leaving nothing running: it
        keeps the snapshot directory open, so the NEXT elaboration fails with
        "Could not open file xsim.dir/tb/xsim.type for writing" and every run
        after that inherits a broken work dir from a timeout minutes earlier.
        """
        if self.proc is None:
            return
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(self.proc.pid)],
                capture_output=True,
                check=False,
            )
        else:
            self.proc.kill()
        with contextlib.suppress(Exception):  # already gone, or unkillable
            self.proc.wait(timeout=10)

    def close(self):
        if self.proc is not None:
            try:
                self.proc.stdin.write("exit\n")
                self.proc.stdin.flush()
                self.proc.wait(timeout=15)
            except Exception:  # noqa: BLE001 - busy or wedged; take the tree
                self._kill_tree()
            finally:
                self.proc = None
        # The snapshot STAYS. It is what the next run reuses, and rebuilding it
        # costs the whole cold build against one stat per source file to decide
        # that it is current.
        self._release()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- driving -------------------------------------------------------
    def _reader(self):
        """Pump xsim's stdout into a queue so reads can time out.

        A bare readline on a pipe cannot be given a deadline on Windows, and
        without one a stalled simulation is indistinguishable from a slow one:
        the caller waits for the bench's own watchdog, which is measured in
        simulated cycles and can be half an hour of wall clock.
        """
        for ln in self.proc.stdout:
            self._q.put(ln.rstrip("\n"))
        self._q.put(None)

    def _tcl(self, line, timeout=None):
        """Send one Tcl command, collect output up to the sentinel."""
        if self.proc is None or self.proc.poll() is not None:
            raise SimError("xsim is not running")
        budget = timeout if timeout is not None else self.timeout
        started = time.time()
        deadline = started + budget
        self.proc.stdin.write(f"{line}\nputs {SENTINEL}\n")
        self.proc.stdin.flush()
        out = []
        last = started  # when the simulator last said anything
        while True:
            left = deadline - time.time()
            if left <= 0:
                self.close()
                raise SimError(_timeout_message(budget, time.time() - last, out))
            try:
                ln = self._q.get(timeout=min(left, 1.0))
            except queue.Empty:
                continue
            if ln is None:
                raise SimError("xsim exited unexpectedly:\n" + "\n".join(out[-40:]))
            if ln.strip() == SENTINEL:
                return out
            out.append(ln)
            last = time.time()

    def run(
        self,
        m,
        k,
        n,
        seed=0xC0FFEE,
        dist="lowrank",
        ncl=None,
        preq=bench.PREQ,
        use=None,
        packing="spread",
    ):
        """Build a problem of this shape, simulate it, and score the result.

        ``m, k, n`` -- the project's shape order. Three bare integers, so a
        transposition here is a different GEMM that runs and answers.

        ``ncl`` is HOW MANY CLUSTERS THE MACHINE HAS. It sizes the mesh and the
        cluster array and is compiled in, so changing it re-elaborates the
        snapshot: the run costs the cold build rather than the warm restart.
        The result says which happened. Running the same shape on two clusters
        and on eight is how a fault that only appears under concurrency is told
        apart from one that depends on problem size -- the two are otherwise
        indistinguishable from the outside, because growing the problem also
        grows the overlap.

        ``use`` is HOW MANY OF THEM THIS PROGRAM KICKS, and it is free. The
        dispatch list is data, not structure, so a subset runs on the mesh that
        is already warm and the rest simply idle -- which is what makes the
        cluster count a control rather than a rebuild. ``packing`` chooses
        WHICH subset: `spread` claims a memory port per cluster before sharing
        any, `packed` fills rows first. At NCL=8 that is the difference between
        two clusters with a port each and two sharing one, so it changes the
        answer and is reported rather than assumed.

        ``preq`` says which of (A, B) were quantised on the way into memory.
        The ANSWER must not depend on it -- the same circuit converts either
        way -- so running both is a direct check that the pre-quantised fetch
        path is faithful, not merely faster.
        """
        if dist not in bench.DISTRIBUTIONS:
            raise SimError(f"unknown distribution {dist!r}")

        with self.lock:
            # Under the lock, because it rebinds module state that the build
            # below reads: two callers changing the count at once would build a
            # problem for one machine and simulate it on another.
            if ncl is not None and ncl != len(bench.CLUSTERS):
                if ncl not in bench.CLUSTER_COUNTS:
                    raise SimError(
                        f"{ncl} clusters is not a mesh this bench builds; "
                        f"use one of {list(bench.CLUSTER_COUNTS)}"
                    )
                bench.use_clusters(ncl)
            ncl = len(bench.CLUSTERS)
            use = ncl if use is None else int(use)
            why = bench.check(m, k, n, ncl, preq, use, packing)
            if why:
                raise SimError("; ".join(why))

            # NCL is part of the snapshot's identity, so a change to it leaves
            # the RUNNING simulator stale -- and `start` returns early while a
            # process exists, so nothing else would notice.
            cold = not self.snapshot_is_current()
            if cold and self.proc is not None:
                self.close()
            self.start()
            prob = bench.build(
                m, k, n, seed, ncl=ncl, dist=dist, preq=preq, use=use, packing=packing
            )
            bench.write_files(self.work, prob)

            t0 = time.time()
            # `restart` re-executes the initial blocks, which is what re-reads
            # the two files just written. The first run has nothing to restart.
            budget = self._budget(prob)
            out = self._tcl(
                "run -all" if not self.started else "restart; run -all",
                timeout=budget,
            )
            self.started = True
            elapsed = time.time() - t0

            got = bench.read_result(self.work, prob)

        return payload(
            prob,
            got,
            out,
            asked={"m": m, "k": k, "n": n},
            seed=seed,
            dist=dist,
            cold=cold,
            seconds=elapsed,
            budget=budget,
        )


def payload(prob, got, log, asked, seed, dist, cold, seconds, budget):
    """Everything a front end is given about one run.

    SEPARATE FROM THE SIMULATOR ON PURPOSE. Every field below is a pure
    function of the problem, the answer and the bench's printed log -- so the
    whole payload the page draws from can be built, and checked, without
    starting anything. It could not be before, and it was the only part of the
    driver with no test at all.
    """
    # References, all accumulating in FP32 except FP64 itself, so the only
    # thing that differs between them is the operand format.
    want = formats.matmul_fp64(prob.a, prob.bt)
    refs = {
        "fp64": want,
        "fp16/fp32": formats.matmul_fp16_fp32(prob.a, prob.bt),
        "bf16/fp32": formats.matmul_bf16_fp32(prob.a, prob.bt),
        "fp16/fp16": formats.matmul_fp16_fp16(prob.a, prob.bt),
        "mxfp8/fp32": formats.matmul_mxfp8_fp32(prob.a, prob.bt),
        "mxfp7 model": mxfp7.model_matmul(prob.a, prob.bt),
    }
    abs_err = np.abs(got - want)
    rel_err = np.where(want != 0, abs_err / np.abs(want), np.inf)
    tel = _telemetry(log)
    # Non-finite where the reference is exactly zero. Sorting and drawing
    # both need a number, and zero is the reading the panel has always
    # used -- an element whose reference is zero has no relative error, it
    # has an undefined one.
    rel_plot = np.where(np.isfinite(rel_err), rel_err, 0.0)
    err_hw = _stats(got, refs["mxfp7 model"])
    err_total = _stats(got, want)
    ok = any("ORCH-OK" in ln for ln in log)

    return {
        "shape": {"m": prob.m, "k": prob.k, "n": prob.n},
        "asked": dict(asked),
        "seed": seed,
        "dist": dist,
        "preq": list(prob.kern.preq),
        "tile": str(prob.tile),
        "ncl": prob.ncl,
        # The clock every derived rate below assumes. Shipped rather than
        # written into the viewer, because a page carrying its own copy
        # disagrees with this one silently the day the target moves.
        "frequency": F_TARGET,
        # What the run was ALLOWED to take, next to what it took. A run
        # close to its budget is the thing to notice before the budget
        # starts firing on healthy work.
        "budget_seconds": round(budget, 1),
        # Whether this run paid the elaboration. The cluster count is
        # compiled in, so changing it is the one control on the page that
        # costs ~16 s instead of ~1 s.
        "cold": cold,
        "passes": len(prob.kern.passes),
        "rounds": len(prob.kern.rounds),
        # THE WHOLE LISTING. It was cut at eight passes, which on anything
        # past the smallest shape showed the first round of one cluster and
        # an ellipsis -- and the tiling is exactly what the listing is for.
        "kernel": prob.kern.listing(),
        "seconds": round(seconds, 2),
        "log": log,
        "telemetry": tel,
        "profile": _profile(tel, prob.m, prob.k, prob.n, prob.ncl),
        "listing": disasm.listing(prob.commands),
        "dispatches": disasm.dispatches(prob.commands),
        "ncmd": len(prob.commands),
        "memory": _memory_map(prob),
        "tiling": _tiling(prob),
        # How the clusters are wired to each other and to memory, and how
        # each cluster's L1 is divided. Both are derived where the machine
        # is described rather than in the browser.
        #
        # FROM THE MESH, NOT FROM THE DISPATCHED COUNT. Those differ whenever a
        # program kicks a subset, and drawing the smaller one would show a
        # machine that does not exist -- a 2x2 picture of a 4x4 device, with the
        # idle clusters and the ports they are still attached to simply absent.
        "mesh": bench.mesh(prob.mesh_ncl),
        # Which of that mesh's clusters were kicked, which idled, and how many
        # memory ports they landed on.
        "dispatch": prob.disp,
        "l1": _l1(prob),
        # Measured, not modelled: when each cluster was filling, sweeping
        # and draining, so overlap can be SEEN rather than inferred from
        # two totals that happen to exceed the elapsed time.
        "timeline": _timeline(tel),
        # A corner of each operand and of both answers, at full precision.
        # The maps below are decimated, so this is where an actual NUMBER
        # can still be read -- which is what the text front ends print and
        # the only way to see that real values went in rather than a
        # plausible-looking picture of them.
        "a_peek": prob.a[:8, :8].tolist(),
        "bt_peek": prob.bt[:8, :8].tolist(),
        "c_peek": got[:PEEK, :PEEK].tolist(),
        "want_peek": want[:PEEK, :PEEK].tolist(),
        # DECIMATED, and they say so. A 512x1024 problem is half a million
        # elements per matrix; shipping ten of them was ~100 MB of JSON and
        # the page then called Math.max(...array) on them, which throws
        # above ~100k arguments -- so every shape past the 256-cube failed
        # in the browser. Every STATISTIC below is still computed on the
        # full array; only the picture is reduced, and `block` says by how
        # much so the caption can be honest about it.
        "got": _heat(got, "absmax"),
        "want": _heat(want, "absmax"),
        "abs_err": _heat(abs_err, "max"),
        "rel_err": _heat(rel_plot, "max"),
        # The worst elements, chosen HERE. The page used to receive all six
        # reference matrices in full and sort half a million triples in the
        # browser to find twelve rows.
        "worst": _worst(got, want, refs, rel_plot),
        # THE ATTRIBUTION. Hardware-vs-FP64 adds two unrelated things:
        # what MXFP7 costs, and whether the circuit is right. Only the
        # second is a bug, and only splitting them tells you which you
        # are looking at.
        "err_hw": err_hw,
        "err_vs": {k: _stats(v, want) for k, v in refs.items() if k != "fp64"},
        "err_total": err_total,
        "ok": ok,
        # ONE definition of pass, shared with run_matmul.py. Two front ends
        # scoring the same run by different rules is how a page reports
        # PASS on a run the command line fails.
        "verdict": _verdict(ok, err_hw, err_total, tel),
    }


def _stats(got, want):
    """Per-element error only: |got-want| and |got-want|/|want|, elementwise.

    NOTHING here is normalised by another element. A metric divided by the
    tensor peak answers "how big is this against the biggest number in the
    matrix", which is a property of the matrix, not of the element you are
    looking at -- so it cannot tell you whether any particular output is
    usable. Percentiles of the per-element values are reported because one
    number cannot describe a distribution, but every number in it is a
    single element's own error.
    """
    g = np.asarray(got, dtype=np.float64).ravel()
    w = np.asarray(want, dtype=np.float64).ravel()
    a = np.abs(g - w)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(w != 0, a / np.abs(w), np.inf)
    finite = r[np.isfinite(r)]
    if finite.size == 0:
        finite = np.zeros(1)

    def pcts(v, frac=False):
        out = {
            "p50": float(np.percentile(v, 50)),
            "p90": float(np.percentile(v, 90)),
            "p99": float(np.percentile(v, 99)),
            "max": float(v.max()),
            "mean": float(v.mean()),
        }
        if frac:
            # how many elements are individually bad, which a percentile
            # cannot say: p90 tells you where the 90th element sits, not how
            # many sit past a threshold you actually care about
            out["over1"] = float((v > 0.01).mean())
            out["over10"] = float((v > 0.10).mean())
            # THE COUNTS TOO, and the population they are out of. The rate is
            # what stays put across shapes and cluster counts -- measured at
            # 3.05e-4, 2.75e-4 and 3.03e-4 on the 2, 4 and 8-cluster references
            # -- while `max` moves with the shape and invites the conclusion
            # that concurrency broke something. A bare fraction cannot be
            # checked against a run's own output; a count can.
            out["over1_n"] = int((v > 0.01).sum())
            out["over10_n"] = int((v > 0.10).sum())
        out["n"] = int(v.size)
        return out

    return {"abs": pcts(a), "rel": pcts(finite, frac=True), "n": int(g.size)}


# Cells a heat map may carry to the browser. 256x256 is the largest problem
# whose full matrix the page ever handled, so it is the size everything above
# is reduced TO rather than an arbitrary budget.
HEAT_MAX_CELLS = 256 * 256

# Corner of C carried at full precision, for the readers that print numbers
# rather than pictures. 64 covers `repl.py`'s widest view.
PEEK = 64


def _heat(mat, how):
    """A matrix reduced to something a browser can hold, and honest about it.

    Reduced by a power-of-two block so the grid stays aligned to the tiling --
    an off-by-one block factor puts a tile boundary in the middle of a cell and
    the picture stops lining up with the tiling panel above it.

    ``how`` picks what a block reports. ``max`` for error maps, because the
    thing worth seeing is the worst element in the neighbourhood and a mean
    would average it away. ``absmax`` for signed value maps: the element of
    largest magnitude, sign kept, so a diverging map keeps both extremes.
    """
    rows, cols = mat.shape
    block = 1
    while (-(-rows // block)) * (-(-cols // block)) > HEAT_MAX_CELLS:
        block *= 2
    if block > 1:
        pr, pc = (-rows) % block, (-cols) % block
        # Padded with zeros, which is inert for both reductions: `max` is over
        # non-negative error and `absmax` cannot pick a zero over a real
        # element unless the whole block is padding, and only whole blocks of
        # padding could do that -- which the ceiling above never produces.
        mat = np.pad(mat, ((0, pr), (0, pc)))
        v = mat.reshape(mat.shape[0] // block, block, mat.shape[1] // block, block)
        if how == "absmax":
            hi, lo = v.max(axis=(1, 3)), v.min(axis=(1, 3))
            mat = np.where(hi >= -lo, hi, lo)
        else:
            mat = v.max(axis=(1, 3))
    return {
        "rows": rows,
        "cols": cols,
        "block": block,
        "how": how,
        "data": mat.tolist(),
    }


# Rows in the worst-element table.
WORST_ROWS = 16


def _worst(got, want, refs, rel):
    """The worst elements by relative error, with every format side by side.

    Chosen here because choosing them in the browser meant shipping every
    reference matrix in full -- six more copies of the problem -- to produce
    sixteen rows. `argpartition` is linear, so this costs nothing next to the
    matmuls that produced the references.
    """
    flat = rel.ravel()
    n = min(WORST_ROWS, flat.size)
    pick = np.argpartition(flat, -n)[-n:]
    pick = pick[np.argsort(flat[pick])[::-1]]
    rows = []
    for f in pick:
        i, j = divmod(int(f), rel.shape[1])
        rows.append(
            {
                "i": i,
                "j": j,
                "hw": float(got[i, j]),
                "want": float(want[i, j]),
                "abs": float(abs(got[i, j] - want[i, j])),
                "rel": float(rel[i, j]),
                "refs": {k: float(v[i, j]) for k, v in refs.items() if k != "fp64"},
            }
        )
    return rows


def _timeline(t):
    """The phase trace, plus what it does and does not cover.

    TWO THINGS THE RAW TRACE CANNOT SAY. The bench stops recording at a fixed
    number of sample windows, so a long run silently produces a SHORT trace --
    drawn without this it looks like a complete picture of a faster machine.
    And the per-cluster phase counters only exist for cluster 0 and the last
    one, so this sampled mask is the only per-cluster measurement there is for
    the ones in between; it is summarised here and labelled as sampled.
    """
    tr = t.get("trace")
    if not tr or not tr.get("cu"):
        return None
    gap = tr["gap"]
    windows = max((len(v) for v in tr["cu"].values()), default=0)
    covered = windows * gap
    # The trace starts when `measure` goes high, which is after the operand
    # upload and before the first round, so it covers the host writes and the
    # run but not the upload.
    measured = t.get("host_cycles", 0) + t.get("run_cycles", 0)
    out = {
        "gap": gap,
        "cu": tr["cu"],
        "windows": windows,
        "covered_cycles": covered,
        "measured_cycles": measured,
        "truncated": bool(measured and covered + gap < measured),
        "cluster": {},
    }
    for c, s in tr["cu"].items():
        busy = [i for i, m in enumerate(s) if m & 0b0111]
        out["cluster"][c] = {
            "windows": len(s),
            # Windows in which the phase was active at all. A window is 64
            # cycles and the phases overlap, so these are not a partition of
            # the time and must not be read as one.
            "fill": sum(1 for m in s if m & 1),
            "gemm": sum(1 for m in s if m & 2),
            "drain": sum(1 for m in s if m & 4),
            "idle": sum(1 for m in s if not m & 0b0111),
            # WHEN this cluster started and stopped working. The spread of
            # these across clusters is the dispatch skew, which is the part of
            # cooperation no total can show.
            "first": busy[0] if busy else None,
            "last": busy[-1] if busy else None,
        }
    return out


# The gate, in one place. run_matmul.py scored a run by these and the page
# scored it by a different rule, so the two could disagree about the same
# simulation -- and the page's rule was the weaker one, missing exactly the
# fault the count bound exists for.
#
# Thresholds and their reasoning: run_matmul.py's module docstring for the
# argument, docs/system.md s3 and docs/isa/memory.md s6.1 for the measurements.
def _round_fail_text(tel):
    """Where a wedged round stopped, in one line, or that it just never said."""
    rf = tel.get("round_fail")
    if not rf:
        return "no ORCH-OK reported"
    where = (
        f", {rf['retired']} of {rf['commands']} commands after " f"{rf['cycles']} cyc"
        if "retired" in rf
        else ""
    )
    return f"ROUND-FAIL round {rf['round']} at pc {rf['pc']}{where}"


def _verdict(ok, err_hw, err_total, tel):
    """PASS plus every criterion and the value that met or missed it.

    ONLY A BROKEN RUN FAILS. A run fails when the machine did not finish, when
    an RTL assertion fired, or when nothing usable came out -- a hang, an error,
    or no answer. Precision is REPORTED against a reference figure and does not
    gate, because the thresholds were fitted to one family of shapes and do not
    hold across all of them: an output that cancels against its own partial sums
    has a large relative error and a correct circuit. Failing on that trains the
    reader to ignore the verdict, which costs more than it catches.

    Each check carries `gates`. The ones that gate are the ones that mean the
    run did not happen; the rest are measurements shown beside a reference.
    """
    hw, tot = err_hw["rel"], err_total["rel"]
    # Nothing usable came out. `p50` is only NaN when every element is
    # non-finite, which is what a dead datapath or an unwritten C region looks
    # like -- the case that used to be caught, accidentally, by a threshold.
    empty = math.isnan(hw["p50"]) or hw["n"] == 0
    checks = [
        {
            "name": "orchestrator finished",
            "value": 1.0 if ok else 0.0,
            "limit": None,
            "gates": True,
            "pass": bool(ok),
            "text": "ORCH-OK" if ok else _round_fail_text(tel),
            "why": "the bench reports its own completion, and names the "
            "control step when a round does not retire",
        },
        {
            "name": "produced an answer",
            "value": 0.0 if empty else 1.0,
            "limit": None,
            "gates": True,
            "pass": not empty,
            "text": "no finite output" if empty else f"{hw['n']} elements",
            "why": "a hang or a dead datapath leaves C unwritten; that is a "
            "failure, where a large error on a real answer is not",
        },
        # ---- reported, not gating -------------------------------------------
        {
            "name": "vs mxfp7 model p50",
            "value": hw["p50"],
            "limit": 1e-3,
            "gates": False,
            "pass": hw["p50"] < 1e-3,
            "text": f"{hw['p50']:.2e}",
            "why": "is the CIRCUIT right -- both sides see the same format. "
            "The most sensitive number here: a mis-slotted fetch moved "
            "it 1.70e-4 -> 1.93e-4",
        },
        {
            "name": "vs mxfp7 model worst",
            "value": hw["max"],
            "limit": 10.0,
            "gates": False,
            "pass": hw["max"] < 10.0,
            "text": f"{hw['max']:.2e}",
            "why": "shape and seed, not correctness -- an output that cancels "
            "against its own partial sums is large here and right",
        },
        {
            "name": "elements over 10%",
            "value": hw["over10"],
            "limit": 5e-4,
            "gates": False,
            "pass": hw["over10"] < 5e-4,
            "text": f"{hw['over10_n']} of {hw['n']}",
            "why": "the COUNT carries the signal: a lost sub-tile corrupts 16 "
            "at once, which the worst element cannot distinguish from "
            "cancellation",
        },
        {
            "name": "vs fp64 p50",
            "value": tot["p50"],
            "limit": 1e-2,
            "gates": False,
            "pass": tot["p50"] < 1e-2,
            "text": f"{tot['p50']:.2e}",
            "why": "what the FORMAT costs, not whether the circuit is right",
        },
    ]
    errors = tel.get("errors", [])
    if errors:
        checks.append(
            {
                "name": "RTL assertions",
                "value": float(len(errors)),
                "limit": 0.0,
                "gates": True,
                "pass": False,
                "text": f"{len(errors)} reported",
                "why": "the bench names its own cause; never suppressed",
            }
        )
    # ONLY THE GATING CHECKS DECIDE. A precision figure outside its reference is
    # reported and does not fail the run: the thresholds fit one family of
    # shapes, and failing a correct circuit on a shape they do not fit teaches
    # the reader to disbelieve the verdict.
    gating = [c for c in checks if c.get("gates")]
    return {
        "pass": all(c["pass"] for c in gating),
        "checks": checks,
        # What a reader should look at even on a PASS: a reference missed is
        # not a failure, but it is not nothing either.
        "advisories": [
            c["name"] for c in checks if not c.get("gates") and not c["pass"]
        ],
    }


def _inst(flit, i, p):
    """One decoded instruction of a pass, plus where it sits in that pass."""
    d = disasm.decode_flit(flit)
    d["slot"] = i
    # The lent flits are the ones the prefetch moved in, and they sit
    # immediately before this pass's DRAIN, which is always its last flit.
    d["lent"] = len(p.flits) - 1 - p.lent <= i < len(p.flits) - 1
    return d


def _tiling(prob):
    """The kernel's output partition, as data rather than as prose.

    Which cluster computes which tile of C, in which round, IS the scheduling
    decision -- and it is the one thing neither the instruction listing nor a
    bar chart can show, because both have already thrown away the geometry.
    Emitted structurally so the page draws C and colours it, rather than
    re-parsing `Kernel.listing()`.

    `(mo, no)` is a GLOBAL output tile: the grid is dealt across clusters, so
    two passes of the same cluster need not be adjacent and a cluster's tiles
    are not a column band. Drawing them by cluster index -- which is what the
    band layout allowed -- would now put half of them in the wrong place.
    """
    t = prob.tile
    order = {c: i for i, c in enumerate(prob.kern.clusters)}
    passes = []
    for ri, rnd in enumerate(prob.kern.rounds):
        for p in rnd.passes:
            ci = order[p.cluster]
            c0 = p.no * t.n
            r0 = p.mo * t.m
            passes.append(
                {
                    "round": ri,
                    "cluster": ci,
                    "cx": p.cluster[0],
                    "cy": p.cluster[1],
                    "mo": p.mo,
                    "no": p.no,
                    "row0": r0,
                    "row1": min(r0 + t.m, prob.m),
                    "col0": c0,
                    "col1": min(c0 + t.n, prob.n),
                    "flits": len(p.flits),
                    "prefetch": p.prefetch_len,
                    "text": list(p.text),
                    # THE PROGRAM, DECODED, against the tile it computes. The
                    # flits are decoded rather than read off the planner, so
                    # what is shown is what the CU will execute -- the same
                    # reason test_kernel_banking decodes them. `lent` marks the
                    # trailing fills that belong to the NEXT pass: the prefetch
                    # moves them in front of this pass's barrier, and counting
                    # them against this tile would misread the schedule.
                    "inst": [_inst(f, i, p) for i, f in enumerate(p.flits)],
                    "lent": p.lent,
                }
            )
    return {
        "m": prob.m,
        "k": prob.k,
        "n": prob.n,
        # The dispatch grid. `tiles` against the cluster count is what says
        # whether the machine can be filled at all -- fewer tiles than clusters
        # and some are simply not kicked.
        "grid": {
            "m_tiles": prob.m // t.m,
            "n_tiles": prob.n // t.n,
            "tiles": (prob.m // t.m) * (prob.n // t.n),
            "live": len({p.cluster for p in prob.kern.passes}),
        },
        "tile": {
            "m": t.m,
            "k": t.k,
            "n": t.n,
            "gm": t.gm,
            "gn": t.gn,
            "nk": t.nk,
            "subtiles": t.gm * t.gn,
        },
        "clusters": [{"x": c[0], "y": c[1]} for c in prob.kern.clusters],
        "rounds": len(prob.kern.rounds),
        "passes": passes,
    }


def _l1(prob):
    """How a cluster's L1 is divided, and how much it is refilled.

    The two decisions -- A double-buffered per chunk, B resident across the m
    loop when a whole column band fits -- are what turn a fill into hidden time
    and what stopped B being re-read per m-tile. Neither is visible in a cycle
    histogram: they show up only as a bar that did not grow.

    The entry counts are COUNTED OFF THE PROGRAM, by decoding the fills one
    cluster actually issues, against the number of distinct entries the tiling
    needs. Deriving the fetched figure from the plan instead would report the
    intent rather than the schedule, which is exactly the thing residency can
    silently stop doing.

    THE TILES ONE CLUSTER OWNS, not the ones the problem has. The grid deals
    output tiles out, so how many column bands and m-tiles a cluster sees is a
    property of its share -- taking it from the problem would compare a
    cluster's fills against everyone's entries and report residency broken.
    """
    lp = prob.kern.l1

    fetched = {0: 0, 1: 0}
    first = prob.kern.clusters[0]
    mine = [p for p in prob.kern.passes if p.cluster == first]
    bands = len({p.no for p in mine})
    m_tiles = len({p.mo for p in mine})
    for p in mine:
        for f in p.flits:
            d = disasm.decode_flit(f)
            if d["op_name"] == "FILL":
                fetched[d["sel"]] += d["n"]

    def banks(n, span):
        return [{"index": i, "start": i * span, "entries": span} for i in range(n)]

    b_spans = (
        [
            {"index": ko, "start": ko * lp.chunk_b, "entries": lp.chunk_b, "chunk": ko}
            for ko in range(lp.chunks)
        ]
        if lp.b_resident
        else banks(lp.banks_b, lp.chunk_b)
    )
    return {
        "a": {
            "entries": lp.a_entries,
            "chunk": lp.chunk_a,
            "banks": lp.banks_a,
            "resident": False,
            "spans": banks(lp.banks_a, lp.chunk_a),
            "fetched": fetched[0],
            "unique": m_tiles * lp.chunks * lp.chunk_a,
        },
        "b": {
            "entries": lp.b_entries,
            "chunk": lp.chunk_b,
            "banks": lp.banks_b,
            "resident": lp.b_resident,
            "spans": b_spans,
            "fetched": fetched[1],
            "unique": bands * lp.chunks * lp.chunk_b,
        },
        "chunks": lp.chunks,
        "fused": lp.fused,
        "bands": bands,
        "m_tiles": m_tiles,
        "cluster": {"x": first[0], "y": first[1]},
    }


def _memory_map(prob):
    """The regions of the device memory image, in 256-bit words.

    Worth showing: overlapping two regions is silent -- the machine computes
    happily on operands another region has overwritten -- so seeing the layout
    is the only cheap way to notice. The sizes are derived the same way
    bench.plan derives them, because a map that disagrees with the layout the
    machine actually used is worse than no map. That includes the STORED
    format: a pre-quantised operand occupies half the words.
    """
    nk = prob.k // 32
    lay = prob.layout
    preq_a, preq_b = prob.kern.preq
    a_words = (prob.m // 4) * nk * tensor.entry_words(preq_a)
    b_words = (prob.n // 4) * nk * tensor.entry_words(preq_b)
    c_words = (prob.m // 4) * (prob.n // 4)

    def fmt(preq):
        return "MXFP7" if preq else "FP16"

    # THREE REGIONS, whatever the cluster count. B and C used to be cut into
    # one region per cluster because a cluster owned a column band of the
    # output; the grid hands out tiles instead, so every cluster addresses the
    # same image and a per-cluster region would name something that no longer
    # exists.
    return {
        "total": bench.WORDS,
        "regions": [
            {
                "name": "A",
                "start": lay.a_word,
                "words": a_words,
                "what": f"{(prob.m // 4) * nk} L1 entries, {fmt(preq_a)}",
            },
            {
                "name": "B",
                "start": lay.b_word,
                "words": b_words,
                "what": f"{(prob.n // 4) * nk} L1 entries, {fmt(preq_b)}",
            },
            {
                "name": "C",
                "start": lay.c_word,
                "words": c_words,
                "what": f"{c_words} sub-tiles, FP16",
            },
        ],
    }


OCCUPANCY = re.compile(
    r"OCCUPANCY cu0 (\d+)\.\.(\d+)\s+cu1 (\d+)\.\.(\d+)\s+overlap (\d+) cycles"
)

COUNTERS = re.compile(
    r"cu0 f=(\d+) g=(\d+) d=(\d+)\s+cu1 f=(\d+) g=(\d+) d=(\d+)\s+"
    r"mem rd=(\d+) wr=(\d+)"
)

MAGSTATE = re.compile(
    r"MAGSTATE idle=(\d+) qfill=(\d+) qwait=(\d+) qemit=(\d+) rd=(\d+) "
    r"wr=(\d+) host=(\d+)"
)

STALL = re.compile(r"STALL in_bp=(\d+) out_bp=(\d+) cu_send=(\d+) cu_dry=(\d+)")

FETCH = re.compile(r"FETCH lat=(\d+) entries=(\d+) floor=(\d+)")

# The operand upload, which is where pre-quantisation is actually paid: once
# per tensor rather than once per read.
UPLOAD = re.compile(r"UPLOAD regions=(\d+) beats=(\d+) cycles=(\d+)")

# Flits across MAG's memory port. One per cycle is the port's whole capacity,
# so these over the run ARE its utilisation.
NOC = re.compile(r"NOC in=(\d+) out=(\d+) ports=(\d+) chans=(\d+)")

# The working pipeline in TIME: one hex nibble per cluster per sample window,
# bit0 fill, bit1 gemm, bit2 drain, bit3 idle. A mask rather than a state,
# because the phases overlap and a sample forced to pick one would hide exactly
# the overlap worth looking at.
TRACE = re.compile(r"TRACE cu(\d+) gap=(\d+) ([0-9a-fA-F]*)")

# A state histogram says WHICH state; these say why it could not leave it.
WHY = re.compile(
    r"WHY wack_nob=(\d+) wack_blk=(\d+) wslot_full=(\d+) rfill_dry=(\d+) "
    r"rwait_emit=(\d+) emit_bp=(\d+) cu_dwait=(\d+) cu_gwait=(\d+)"
)

PROFILE = re.compile(
    r"PROFILE host=(\d+) run=(\d+) fill=(\d+) gemm=(\d+) drain=(\d+) "
    r"idle=(\d+) rd=(\d+) wr=(\d+) beat=(\d+)"
)

# How a round dies, printed at the moment the bench detects it. The command
# index says WHICH control step never retired, which is the difference between
# a diagnosis and a bisect.
ROUND_FAIL = re.compile(r"ROUND-FAIL round (\d+) stopped at pc=(\d+)")
ROUND_FAIL_WHERE = re.compile(
    r"after (\d+) cycles, (\d+) of (\d+) commands, (\d+) idle"
)

# The design's target clock. Everything below is reported in cycles first and
# seconds second, because cycles are what the RTL actually produced and
# seconds are an assumption about closing timing at this frequency.
F_TARGET = 300e6
MACS_PER_CLUSTER = 512  # 4 TCU x 4x8x4, one 4x4 sub-tile x 32 K per cycle

# WHAT EACH CYCLE BUCKET IS COUNTED OUT OF. Not decoration: the buckets below
# come from three different populations and dividing all of them by the run
# produced percentages that were not comparable and, for the port-summed ones,
# not percentages at all.
#
#   ports     summed over MAG's memory ports -- the bench's loops run
#             `for (mq = 0; mq < MEMP; ...)`, so at four ports the raw count
#             reaches four per cycle. Out of run x ports it is a fraction of
#             that resource; out of run alone it read past 400%.
#   cluster0  tapped on ONE cluster -- `cst[0]`, `csend_v[0]` -- so it is a
#             fraction of one CU's time and says nothing about the others.
#   machine   a single unit, out of the run.
#
# The page shows `frac` and names the basis. It used to show all of them as
# identical bars and sum the MAG ones into a "% busy" figure, which added
# deliberately overlapping buckets across ports -- the same mistake as summing
# the resource budgets, in a different panel.
MAG_BASIS = {
    "idle": "ports",
    "qfill": "ports",
    "qwait": "ports",
    "qemit": "ports",
    "rd": "ports",
    "wr": "ports",
    "host": "machine",
}
# `in_bp` COUNTS REFUSALS, NOT CONGESTION -- and it stopped meaning what its
# name suggests when the agent moved onto the memory ports. A port now asserts
# busy whenever the offered flit belongs to the other consumer (the engine
# declining a control flit, or the agent declining a memory one), which happens
# constantly and costs nothing: it went 0.0% -> 39.8% on a 2-cluster run whose
# cycle count did not move at all. Reading it as pressure sends the reader
# hunting for a bottleneck that is not there -- which is the third time a
# derived percentage in this design has changed meaning under its own name.
STALL_BASIS = {
    "in_bp": "ports",
    "out_bp": "ports",
    "cu_send": "cluster0",
    "cu_dry": "cluster0",
}
WHY_BASIS = {
    "wack_nob": "ports",
    "wack_blk": "ports",
    "wslot_full": "ports",
    "rfill_dry": "ports",
    "rwait_emit": "ports",
    "emit_bp": "ports",
    "cu_dwait": "cluster0",
    "cu_gwait": "cluster0",
}

# Runs measured this session against the current RTL, for the page to offer as
# presets and to compare a fresh run against. Recorded numbers, and the page
# must label them as recorded -- they are the value of a comparison only for as
# long as it is obvious they are not this run's.
#
# SHAPES ARE M, K, N. All three have K=256, which is why the K-heavy regime was
# never measured and the N-only dispatch went unnoticed for as long as it did.
MEASURED = [
    {
        "ncl": 2,
        "m": 256,
        "k": 256,
        "n": 256,
        "cycles": 18701,
        "gflops": 538.3,
        "utilisation": 0.876,
        "p50": 1.70e-4,
        "max": 1.00e0,
        "over1_n": 20,
        "over10_n": 4,
        "elements": 65536,
    },
    {
        "ncl": 4,
        "m": 256,
        "k": 256,
        "n": 512,
        "cycles": 20647,
        "gflops": 975.1,
        "utilisation": 0.794,
        "p50": 1.70e-4,
        "max": 2.43e0,
        "over1_n": 36,
        "over10_n": 7,
        "elements": 131072,
    },
    {
        "ncl": 8,
        "m": 512,
        "k": 256,
        "n": 1024,
        "cycles": 43382,
        "gflops": 1856.3,
        "utilisation": 0.755,
        "p50": 1.71e-4,
        "max": 2.43e0,
        "over1_n": 159,
        "over10_n": 49,
        "elements": 524288,
    },
]


def _bucket(t, prefix, basis, run, ports):
    """Cycle buckets as a fraction OF THE RESOURCE THEY WERE COUNTED ON."""
    out = {}
    for k, b in basis.items():
        cycles = t.get(f"{prefix}{k}", 0)
        cap = run * ports if b == "ports" else run
        out[k] = {
            "cycles": cycles,
            "frac": cycles / cap if cap else 0.0,
            "basis": b,
        }
    return out


def _profile(t, m, k, n, nclusters):
    """Turn the bench's cycle buckets into bandwidth and rate.

    Reported per PHASE because the interesting question for a dataflow machine
    is not how fast it multiplies but how much of the time it is allowed to.
    """
    run = t.get("run_cycles", 0)
    if not run:
        return None
    beat = t.get("beat_bytes", 32)
    macs = m * k * n
    peak = MACS_PER_CLUSTER * nclusters
    # MAG's memory ports. Every per-port counter below is a SUM over them, so
    # the capacity they are measured against scales with the count.
    ports = max(1, t.get("mem_ports", 1))

    def rate(cycles):
        return macs / cycles if cycles else 0.0

    rd_bytes = t["rd_beats"] * beat
    wr_bytes = t["wr_beats"] * beat

    # Bytes per cycle -> bytes per second at the target clock.
    def bw(nbytes):
        return nbytes / run * F_TARGET / 1e9 if run else 0.0

    # Out of the cycles the machine was actually running, times the number of
    # clusters -- NOT out of the buckets' own sum. A GEMM now retires when its
    # sweep starts, so a CU can be filling and sweeping in the same cycle and
    # the buckets genuinely overlap. Normalising by their sum would silently
    # rescale every phase whenever the overlap improved, which is the one thing
    # this measurement exists to show.
    total = run * nclusters
    return {
        "cycles": {
            # `operand_upload` is the FP16 push through MAG's memory window,
            # paid once per TENSOR. `host_upload` is the per-ROUND control and
            # instruction traffic. Keeping them apart is what makes the
            # pre-quantisation trade visible: one grows with the operands, the
            # other with the number of passes.
            "operand_upload": t.get("upload_cycles", 0),
            "operand_beats": t.get("upload_beats", 0),
            "host_upload": t["host_cycles"],
            "run": run,
            "total": t["host_cycles"] + run + t.get("upload_cycles", 0),
        },
        # Occupancy is summed over CUs, so it is out of run*nclusters.
        "occupancy": {
            phase: {
                "cycles": t[f"{phase}_cycles"],
                "frac": t[f"{phase}_cycles"] / total if total else 0.0,
            }
            for phase in ("fill", "gemm", "drain", "idle")
        },
        "traffic": {
            "read_bytes": rd_bytes,
            "write_bytes": wr_bytes,
            "read_gbps": bw(rd_bytes),
            "write_gbps": bw(wr_bytes),
            "beat_bytes": beat,
            # An output-stationary schedule should write C exactly once.
            "c_write_amplification": wr_bytes / (m * n * 2) if m * n else 0.0,
        },
        # PER COMPONENT, not just end to end. MAG serves one NoC memory
        # request at a time PER PORT, so this histogram is the memory system's
        # whole throughput budget -- and `idle` is the part nobody claimed.
        # The buckets deliberately overlap: the read engine runs concurrently
        # with the write and host paths, so they do not partition the time and
        # must never be summed into a "busy" figure.
        "mag": _bucket(t, "mag_", MAG_BASIS, run, ports),
        # Cycles LOST rather than spent: a producer holding a flit it cannot
        # hand over, or a consumer with nothing to take.
        "stalls": _bucket(t, "stall_", STALL_BASIS, run, ports),
        "fetch": {
            "latency": t.get("fetch_lat", 0),
            # COUNTED ON CLUSTER 0 ONLY -- `cl1_we[0]` -- while `fill_cycles`
            # is occupancy summed over every cluster. Dividing one by the other
            # without the count reports N times the real per-entry cost, which
            # is what the command line was doing with a hard-coded 2.
            "entries": t.get("fetch_entries", 0),
            "entries_all": t.get("fetch_entries", 0) * nclusters,
            "floor": t.get("fetch_floor", 4),
            "cycles_per_entry": (
                t["fill_cycles"] / (t.get("fetch_entries", 0) * nclusters)
                if t.get("fetch_entries")
                else 0.0
            ),
        },
        # Not "which state" but "why it could not leave it". A component that
        # got slower has a reason, and the reason is one of these.
        "why": _bucket(t, "why_", WHY_BASIS, run, ports),
        # WHAT FRACTION OF EACH RESOURCE IS IN USE, side by side. A rate on its
        # own does not say whether the machine is compute-bound, bus-bound or
        # simply idle.
        #
        #   flops   MAC/cycle against 512 per cluster
        #   mem_rd  AXI read beats against one per cycle
        #   mem_wr  AXI write beats against one per cycle
        #   noc_in  flits into MAG's memory port, against one per cycle
        #   noc_out flits out of it, against one per cycle
        #
        # FOUR SEPARATE BUDGETS, AND THEY MUST NOT BE ADDED. Every one of these
        # links is full duplex: AR/R and AW/W are different AXI channels (and
        # `axi_ram` reads and writes in the same cycle from two always blocks),
        # and the NoC's two directions are different wires. Adding them prices
        # capacity that never competes -- it read 91.5% "of data movement" at
        # two clusters, where the real binding figure was mem_rd at 30%.
        #
        # DIVIDED BY THE PORT COUNT, because the counts are sums over MAG's
        # memory ports and each port has its own channel. Dividing by one
        # per cycle reported 101.9% and 110.4% at eight clusters -- an
        # impossible number that looked exactly like a saturated bus, which is
        # the very conclusion the split was made to stop guessing at.
        #
        # What binds is the MAXIMUM, not the sum. Read them that way.
        "resource": {
            "flops": rate(run) / peak if peak else 0.0,
            "mem_rd": t["rd_beats"] / run / ports,
            "mem_wr": t["wr_beats"] / run / ports,
            "noc_in": t.get("noc_in", 0) / run / ports,
            "noc_out": t.get("noc_out", 0) / run / ports,
        },
        "mem_ports": ports,
        "rate": {
            "macs": macs,
            "peak_macs_per_cycle": peak,
            "macs_per_cycle": rate(run),
            "utilisation": rate(run) / peak if peak else 0.0,
            # 2 flops per MAC. At the target clock, and counting only the
            # cycles the machine was running -- host upload is excluded
            # because on real hardware it overlaps the previous round.
            "gflops": 2 * rate(run) * F_TARGET / 1e9,
            "gflops_with_upload": (
                2 * macs / (run + t["host_cycles"]) * F_TARGET / 1e9
            ),
            "seconds": run / F_TARGET,
        },
    }


def _telemetry(lines):
    """Pull the bench's own counters out of its printed log.

    PARSED AGAINST WHAT `mag_driver_tb` ACTUALLY PRINTS. It reports completion
    as `ORCH-OK` and a failure as `ROUND-FAIL`; it has no `DONE code` line and
    no bare `stopped at pc` line. Two parsers here looked for exactly those,
    matched nothing on every run, and the page dutifully rendered "pc — code —"
    as though the machine had declined to say.
    """
    t = {}
    for ln in lines:
        s = ln.strip()
        # The round that did not retire, and the control step it stopped on.
        # This is the bench's whole diagnosis of a wedge and it survived only
        # as a line in the raw log.
        if s.startswith("ROUND-FAIL"):
            w = ROUND_FAIL.match(s)
            if w:
                t["round_fail"] = {"round": int(w.group(1)), "pc": int(w.group(2))}
                t["pc"] = int(w.group(2))
        elif s.startswith("after ") and "commands" in s:
            w = ROUND_FAIL_WHERE.match(s)
            if w and "round_fail" in t:
                t["round_fail"].update(
                    {
                        "cycles": int(w.group(1)),
                        "retired": int(w.group(2)),
                        "commands": int(w.group(3)),
                        "idle": int(w.group(4)),
                    }
                )
        elif "ERROR" in s:
            t.setdefault("errors", []).append(s)
        elif s.startswith("OCCUPANCY"):
            w = OCCUPANCY.match(s)
            if w:
                a0, b0, a1, b1, ov = (int(v) for v in w.groups())
                t["occupancy"] = {
                    "cu0": [a0, b0],
                    "cu1": [a1, b1],
                    "overlap_cycles": ov,
                    "span_ps": max(b0, b1) - min(a0, a1),
                }
        elif s.startswith("TRACE"):
            w = TRACE.match(s)
            if w:
                cu, gap, hexs = int(w.group(1)), int(w.group(2)), w.group(3)
                tr = t.setdefault("trace", {"gap": gap, "cu": {}})
                tr["gap"] = gap
                tr["cu"][cu] = [int(c, 16) for c in hexs]
        elif s.startswith(("MAGSTATE", "STALL", "FETCH", "WHY", "UPLOAD", "NOC")):
            names = {
                "WHY": (
                    WHY,
                    (
                        "why_wack_nob",
                        "why_wack_blk",
                        "why_wslot_full",
                        "why_rfill_dry",
                        "why_rwait_emit",
                        "why_emit_bp",
                        "why_cu_dwait",
                        "why_cu_gwait",
                    ),
                ),
                "MAGSTATE": (
                    MAGSTATE,
                    (
                        "mag_idle",
                        "mag_qfill",
                        "mag_qwait",
                        "mag_qemit",
                        "mag_rd",
                        "mag_wr",
                        "mag_host",
                    ),
                ),
                "STALL": (
                    STALL,
                    ("stall_in_bp", "stall_out_bp", "stall_cu_send", "stall_cu_dry"),
                ),
                "FETCH": (FETCH, ("fetch_lat", "fetch_entries", "fetch_floor")),
                "UPLOAD": (
                    UPLOAD,
                    ("upload_regions", "upload_beats", "upload_cycles"),
                ),
                "NOC": (NOC, ("noc_in", "noc_out", "mem_ports", "mem_chans")),
            }[s.split()[0]]
            w = names[0].match(s)
            if w:
                t.update(dict(zip(names[1], (int(v) for v in w.groups()))))
        elif s.startswith("PROFILE"):
            w = PROFILE.match(s)
            if w:
                t.update(
                    dict(
                        zip(
                            (
                                "host_cycles",
                                "run_cycles",
                                "fill_cycles",
                                "gemm_cycles",
                                "drain_cycles",
                                "idle_cycles",
                                "rd_beats",
                                "wr_beats",
                                "beat_bytes",
                            ),
                            (int(v) for v in w.groups()),
                        )
                    )
                )
        else:
            hit = COUNTERS.match(s)
            if hit:
                names = [
                    "cu0_fill",
                    "cu0_gemm",
                    "cu0_drain",
                    "cu1_fill",
                    "cu1_gemm",
                    "cu1_drain",
                    "mem_rd",
                    "mem_wr",
                ]
                t.update(dict(zip(names, (int(v) for v in hit.groups()))))
    return t
