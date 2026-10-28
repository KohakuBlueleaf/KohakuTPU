"""Check one LeNet/Adam16 training step, kernel by kernel against tinygrad CPU.

Example (third call is TinyJit replay; check-step is zero based)::

    python scripts/py/check/step_kernels.py build/step.json --device card \
        --board multimesh_v9t6 --transport xdma --nodes 1 --library build/templates

--device offline is a library/shape check ONLY, never a numeric pass. --kernel
is a regex over 'index:kind:name'. Unselected kernels still execute so selected
ones see the actual state. --corrupt regex poisons one observed output after
readback (never the oracle) to witness failure and name the affected kernel.
"""

import argparse
import json
import pathlib
import time

import numpy as np
from examples.tinygrad.mnist_mlp import LeNet, train_step
from tinygrad import Tensor
from tinygrad.engine.jit import TinyJit
from tinygrad.helpers import Context
from tinygrad.nn.datasets import mnist
from tinygrad.nn.state import get_parameters

from kohakutpu.application.checks.determinism import DeterminismChecker, RepeatComplete
from kohakutpu.application.checks.step import StepChecker
from kohakutpu.application.ktpugrad.nn import Adam16, place
from kohakutpu.application.runtime import device as D


def record_loss(result, step, value):
    """Deterministic NaN bits are valid evidence, not JSON numeric literals."""
    if value is not None and not np.isfinite(value):
        result.setdefault("nonfinite_losses", []).append(
            dict(step=step, value=str(value))
        )
        value = None
    result["losses"].append(value)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("out", type=pathlib.Path)
    ap.add_argument("--device", choices=D.KINDS, default="sim")
    ap.add_argument("--board", default="vlt_v9t6s_1n")
    ap.add_argument("--build", default=None)
    ap.add_argument("--transport", choices=D.TRANSPORTS, default=None)
    ap.add_argument("--nodes", type=int, default=1)
    ap.add_argument("--library", required=True)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--steps", type=int, default=3)
    ap.add_argument("--check-step", type=int, default=2)
    ap.add_argument("--no-jit", action="store_true")
    ap.add_argument("--kernel", default=None)
    ap.add_argument(
        "--repeat",
        type=int,
        default=None,
        help="bit-exact schedule/package repetition K times (no CPU oracle)",
    )
    ap.add_argument(
        "--repeat-package",
        default=None,
        help="repeat first matching runtime package name prefix --repeat times, then stop",
    )
    ap.add_argument(
        "--corrupt",
        nargs="?",
        const="auto",
        default=None,
        help="inject one failing observation; default target is a naturally passing exact kernel, or supply a regex",
    )
    ap.add_argument(
        "--failure-dir",
        default=None,
        help="save failed inputs/outputs/bounds as NPZ (default OUT.failures)",
    )
    ap.add_argument("--data", choices=("mnist", "synthetic"), default="mnist")
    args = ap.parse_args()
    if args.repeat is not None and args.repeat < 2:
        ap.error("--repeat must be at least 2")
    if args.repeat_package is not None and (
        not args.repeat_package or args.repeat is None
    ):
        ap.error("--repeat-package requires a nonempty prefix and --repeat K")
    if args.repeat is not None and args.corrupt:
        ap.error("--corrupt belongs to CPU comparison, not determinism mode")
    if args.batch < 1 or not 0 <= args.check_step < args.steps:
        ap.error("positive batch and 0 <= check-step < steps required")
    if not args.no_jit and args.check_step < 2:
        ap.error("TinyJit checks must select a replayed call (check-step >= 2)")
    if args.corrupt and args.device == "offline":
        ap.error("offline does not execute: mutation needs an executing device")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    result = dict(
        configuration=vars(args) | {"out": str(args.out)}, rows=[], passed=False
    )
    dev = None
    checker = None
    try:
        dev = D.Device(
            args.device,
            board=args.board,
            build=args.build,
            transport=args.transport,
            nodes=args.nodes,
            library=args.library,
            library_only=True,
        )
        observer = (
            DeterminismChecker(dev, args.repeat, args.kernel, args.repeat_package)
            if args.repeat is not None
            else StepChecker(
                dev,
                args.kernel,
                args.corrupt,
                failure_dir=args.failure_dir or args.out.with_suffix(".failures"),
            )
        )
        with D.attached(dev), observer as checker:
            Tensor.manual_seed(0)
            with Context(DEV="CPU"):
                model = LeNet()
            place(model)
            opt = Adam16(get_parameters(model), lr=2e-3)
            rng = np.random.default_rng(0)
            if args.data == "mnist":
                x, y, _, _ = (t.numpy() for t in mnist(device="CPU"))
                x = (x.reshape(-1, 784) / 255).astype(np.float16)
            else:
                x = rng.standard_normal((128, 784)).astype(np.float16)
                y = rng.integers(0, 10, len(x))
                rng = np.random.default_rng(0)
            eye = np.eye(10, dtype=np.float16)

            def one(xb, yb):
                return train_step(model, opt, xb, yb)

            run_step = one if args.no_jit else TinyJit(one)
            walls, losses = [], []
            result.update(seconds=walls, losses=losses, nonfinite_losses=[])
            print(
                (
                    " idx kind     kernel                         repeats bit-exact status"
                    if args.repeat is not None
                    else " idx kind     kernel                         elements      max_abs      max_rel status"
                ),
                flush=True,
            )
            with Context(TRAINING=1):
                for step in range(args.steps):
                    indices = rng.integers(0, len(x), args.batch)
                    xb = Tensor(x[indices], device="KTPU").realize()
                    yb = Tensor(eye[y[indices]], device="KTPU").realize()
                    checker.active = step == args.check_step
                    t0 = time.monotonic()
                    loss, _logits = run_step(xb, yb)
                    dev.drain()
                    checker.active = False
                    record_loss(
                        result, step, None if args.device == "offline" else loss.item()
                    )
                    walls.append(time.monotonic() - t0)
            result.update(
                checker.report(),
                seconds=walls,
                losses=losses,
                counters=dict(dev.counters),
                numeric=args.device != "offline",
            )
            if not args.no_jit and not checker.graph_replays:
                raise RuntimeError("selected step did not replay a TinyJit graph")
            if not checker.rows:
                raise RuntimeError("no kernel matched; an empty check is not success")
            if args.repeat_package and not checker.package_found:
                raise RuntimeError("no package matched the requested prefix")
            if args.corrupt and checker.corrupted is None:
                raise RuntimeError("corruption hook matched no checked kernel")
            if dev.counters["compile_calls"] or dev.counters["library_misses"]:
                raise RuntimeError("library-only invariant violated")
    except RepeatComplete:
        result.update(
            checker.report(), numeric=args.device != "offline", package_complete=True
        )
        if not args.no_jit and not checker.graph_replays:
            result.update(
                passed=False, error="selected step did not replay a TinyJit graph"
            )
    except Exception as exc:  # noqa: BLE001 -- diagnostic must persist partial evidence
        if checker is not None:
            result.update(checker.report())
        result.update(passed=False, error=f"{type(exc).__name__}: {exc}")
        print(result["error"], flush=True)
    finally:
        try:
            if dev is not None:
                result["counters"] = dict(dev.counters)
                result["numeric"] = args.device != "offline"
                if dev.counters["compile_calls"] or dev.counters["library_misses"]:
                    result["passed"] = False
                    result.setdefault("error", "library-only invariant violated")
                dev.close()
        except Exception as exc:  # noqa: BLE001 -- preserve original failure and JSON
            result["close_error"] = f"{type(exc).__name__}: {exc}"
            result["passed"] = False
            result.setdefault("error", result["close_error"])
        finally:
            args.out.write_text(
                json.dumps(result, indent=2, allow_nan=False), encoding="utf-8"
            )
    failed = result.get("error") or (result.get("numeric") and not result.get("passed"))
    print(
        f"{args.out}: {'FAIL' if failed else 'SHAPE ONLY' if args.device == 'offline' else 'PASS'}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
