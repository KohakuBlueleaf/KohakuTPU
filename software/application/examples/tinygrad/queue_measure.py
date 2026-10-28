"""Raw host submission times and firmware records for eager queued workloads."""

import argparse
import json
import time
from pathlib import Path
from types import SimpleNamespace

import ktpu_pytest_lock
import numpy as np
from examples.tinygrad.mnist_mlp import LeNet
from tinygrad import Tensor
from tinygrad.helpers import Context
from tinygrad.nn.state import get_parameters

from kohakutpu.application.ktpugrad.nn import Adam16, place
from kohakutpu.application.runtime import device as D


def measure(dev, workload, seconds=90, batch=1):
    """One deterministic batch, no dataset download and no silicon attachment."""
    events, submissions = [], []
    original_submit, original_collect = dev.card._submit, dev._collect_one
    begin = time.perf_counter()
    result = {
        "workload": workload,
        "queued": dev.queued,
        "events": events,
        "batch": batch,
        "budget_seconds": seconds,
    }

    def submit(node, raw, bindings, **kw):
        if time.perf_counter() - begin > seconds:
            raise TimeoutError(f"measurement exceeded {seconds}s")
        start = time.perf_counter()
        tag = original_submit(node, raw, bindings, **kw)
        events.append(
            {
                "node": node,
                "tag": tag,
                "host_submit_start": start,
                "host_submit_end": time.perf_counter(),
            }
        )
        return tag

    def collect(pending):
        remaining = seconds - (time.perf_counter() - begin)
        if remaining <= 0:
            raise TimeoutError(f"measurement exceeded {seconds}s")
        dev.card.alarm = remaining
        run = original_collect(pending)
        if not any(pending is s for s in submissions):
            submissions.append(pending)
            tags = getattr(pending.pending, "tags", {})
            for n, (tag, _) in tags.items():
                event = next(e for e in events if e["node"] == n and e["tag"] == tag)
                got = pending.result
                got = got.get("nodes", {}).get(n, got)
                event.update(
                    plan=pending.info.get("plan"),
                    run=pending.name,
                    cycles=run.cycles,
                    phases=got["phases"],
                    host_collect_end=time.perf_counter(),
                )
        return run

    dev.card._submit, dev._collect_one = submit, collect
    try:
        with D.attached(dev):
            Tensor.manual_seed(0)
            if workload == "activation":
                x = Tensor(
                    np.linspace(-1, 1, 1024, dtype=np.float16), device="KTPU"
                ).realize()
                y = x
                for _ in range(4):
                    y = y.silu().contiguous().realize()
            else:
                with Context(DEV="CPU"):
                    model = LeNet()
                place(model)
                x = Tensor(
                    np.linspace(0, 1, batch * 784, dtype=np.float16).reshape(
                        batch, 784
                    ),
                    device="KTPU",
                ).realize()
                if workload == "lenet_forward":
                    y = model(x).realize()
                elif workload == "lenet_train":
                    opt = Adam16(get_parameters(model), lr=2e-3)
                    labels = Tensor(
                        np.eye(10, dtype=np.float16)[np.arange(batch) % 10],
                        device="KTPU",
                    ).realize()
                    with Context(TRAINING=1):
                        y = -(model(x).log_softmax() * labels).sum(-1).mean()
                        opt.zero_grad()
                        opt.scale(y).backward()
                        Tensor.realize(y, *opt.schedule_step())
                else:
                    raise ValueError(workload)
            result["output"] = y.numpy().tolist()
            dev.drain()
    except Exception as error:  # noqa: BLE001 -- keep raw events
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        result["wall_seconds"] = time.perf_counter() - begin
        dev.card._submit, dev._collect_one = original_submit, original_collect
    for i, event in enumerate(events):
        event["host_gap_seconds"] = (
            event["host_submit_start"] - events[i - 1]["host_submit_end"] if i else None
        )
        event["node_idle_cycles"] = event.get("phases", {}).get("idle")
    result["completed_kernels"] = sum("cycles" in e for e in events)
    result["published_kernels"] = len(events)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "workload", choices=("activation", "lenet_forward", "lenet_train")
    )
    parser.add_argument("--seconds", type=float, default=3550)
    parser.add_argument("--batch", type=int, default=64)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="measurement JSON, appended to when it exists",
    )
    args = parser.parse_args()
    if not 0 < args.seconds <= 3600:
        parser.error("seconds must be in (0, 3600]")
    ktpu_pytest_lock.pytest_configure(SimpleNamespace())
    dev = None
    try:
        dev = D.Device(kind="sim")
        result = measure(dev, args.workload, args.seconds, args.batch)
        output = args.output
        data = (
            json.loads(output.read_text(encoding="utf-8"))
            if output.exists()
            else {"measurements": []}
        )
        data["measurements"].append(result)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {k: v for k, v in result.items() if k not in ("events", "output")}
            ),
            flush=True,
        )
    finally:
        if dev is not None:
            dev.card.t.close()
            dev.closed = True
            for arena in dev.memory.arenas():
                arena.close()
        ktpu_pytest_lock.pytest_unconfigure(SimpleNamespace())


if __name__ == "__main__":
    main()
