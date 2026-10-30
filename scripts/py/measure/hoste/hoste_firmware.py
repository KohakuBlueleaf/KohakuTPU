"""Run the HOSTE firmware image vs base firmware on the Verilated v9t6 card model."""

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

import numpy as np
from examples.tinygrad.mnist_mlp import LeNet, train_step
from examples.tinygrad.vit_tiny import seeded_model
from kohakutpu.application import ktpugrad
from kohakutpu.application.ktpugrad.nn import Adam16, place
from kohakutpu.application.runtime import card as card_mod
from kohakutpu.application.runtime import device as D
from tinygrad import Tensor
from tinygrad.helpers import Context
from tinygrad.nn.state import get_parameters

ROOT = Path(__file__).resolve().parents[4]


def array_digest(arr: np.ndarray) -> dict:
    raw = np.ascontiguousarray(arr).tobytes()
    return {
        "shape": list(arr.shape),
        "dtype": str(arr.dtype),
        "bytes": len(raw),
        "nonzero_bytes": int(sum(b != 0 for b in raw)),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def run_vit_block(args) -> dict:
    card_mod.FIRMWARE = args.fw.resolve()
    os.environ["KOHAKUTPU_RESIDENT"] = str(args.resident)
    ktpugrad.install()
    Tensor.manual_seed(0)
    model = seeded_model(1, 17, width=64, image_size=16)
    rng = np.random.default_rng(18)
    x_np = rng.standard_normal((1, 3, 16, 16)).astype(np.float16)
    t0 = time.monotonic()
    with D.attached(
        D.Device("sim", board=args.board, build=args.build, queue_depth=1)
    ) as dev:
        place(model)
        x = Tensor(x_np, device="KTPU").realize()
        with Context(TRAINING=0):
            y = model(x).realize()
        dev.drain()
        out = y.numpy()
        q = dev.card.queues[0]
        list_abi = q.mem.read64(q.base + q.layout.list_abi)
        report = {
            "case": "vit-block",
            "fw": str(args.fw),
            "board": args.board,
            "resident": args.resident,
            "wall_seconds": time.monotonic() - t0,
            "launches": dev.counters["runs"],
            "cycles": dev.counters["cycles"],
            "bytes_in": dev.counters["bytes_in"],
            "bytes_out": dev.counters["bytes_out"],
            "upload_bytes": q.counters["upload_bytes"],
            "sq_tail": q.sq_tail,
            "list_abi": list_abi,
            "output": array_digest(out),
        }
    np.savez(args.output.with_suffix(".npz"), logits=out.view(np.uint8))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report), flush=True)
    return report


def run_list_path(args) -> dict:
    card_mod.FIRMWARE = args.fw.resolve()
    os.environ["KOHAKUTPU_RESIDENT"] = "0"
    os.environ["KOHAKUTPU_PREPARED_REPLAY"] = "1"
    os.environ["KOHAKUTPU_EXECUTION_LIST"] = "1" if args.lists else "0"
    ktpugrad.install()
    Tensor.manual_seed(0)
    model = seeded_model(1, 17, width=64, image_size=16)
    block = model.blocks[0]
    rng = np.random.default_rng(18)
    x0_np = rng.standard_normal((1, 5, 64)).astype(np.float16)
    x1_np = rng.standard_normal((1, 5, 64)).astype(np.float16)
    t0 = time.monotonic()
    with D.attached(
        D.Device("sim", board=args.board, build=args.build, queue_depth=1)
    ) as dev:
        place(block)
        x = Tensor(x0_np, device="KTPU").realize()
        with Context(TRAINING=0):
            # Warmup once so constant tables exist before recording the pure-RUN replay log.
            _ = block(x).realize()
            dev.drain()
            x.uop.buffer.allocator._copyin(
                x.uop.buffer._buf, memoryview(x1_np).cast("B")
            )
            dev.record()
            y = block(x).realize()
            dev.drain()
            log = dev.recorded()
        q = dev.card.queues[0]
        # Zero every form of the output buffer before replay so replay must compute the output.
        for form in y.uop.buffer._buf.forms.values():
            dev.write(form.addr, bytes(form.nbytes))
        cycles_before = dev.counters["cycles"]
        runs_before = dev.counters["runs"]
        sq_before = q.sq_tail
        dev.replay(log)
        dev.drain()
        out = y.numpy()
        sq_entries = q.sq_tail - sq_before
        replay_cycles = dev.counters["cycles"] - cycles_before
        replay_runs = dev.counters["runs"] - runs_before
        list_abi = q.mem.read64(q.base + q.layout.list_abi)
        last_record = (
            q._done[q.sq_tail - 1].record if (q.sq_tail - 1) in q._done else None
        )
        log.close()
        report = {
            "case": "list",
            "fw": str(args.fw),
            "board": args.board,
            "lists": bool(args.lists),
            "wall_seconds": time.monotonic() - t0,
            "recorded_packages": len(log),
            "replay_runs": replay_runs,
            "sq_entries": sq_entries,
            "replay_cycles": replay_cycles,
            "list_abi": list_abi,
            "last_record": last_record,
            "output": array_digest(out),
        }
    np.savez(args.output.with_suffix(".npz"), block_out=out.view(np.uint8))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report), flush=True)
    return report


def run_lenet_step(args) -> dict:
    card_mod.FIRMWARE = args.fw.resolve()
    os.environ["KOHAKUTPU_RESIDENT"] = str(args.resident)
    os.environ["KOHAKUTPU_PREPARED_REPLAY"] = "0"
    os.environ["KOHAKUTPU_EXECUTION_LIST"] = "0"
    ktpugrad.install()
    Tensor.manual_seed(0)
    with Context(DEV="CPU"):
        model = LeNet()
    rng = np.random.default_rng(18)
    x_np = rng.standard_normal((1, 784)).astype(np.float16)
    y_np = np.eye(10, dtype=np.float16)[[0]]
    t0 = time.monotonic()
    arrays = {}
    with D.attached(
        D.Device("sim", board=args.board, build=args.build, queue_depth=1)
    ) as dev:
        place(model)
        opt = Adam16(get_parameters(model), lr=2e-3)
        xb = Tensor(x_np, device="KTPU").realize()
        yb = Tensor(y_np, device="KTPU").realize()
        with Context(TRAINING=1):
            loss, logits = train_step(model, opt, xb, yb)
        dev.drain()
        arrays["loss"] = loss.numpy()
        arrays["logits"] = logits.numpy()
        for idx, p in enumerate(get_parameters(model)):
            arrays[f"param_{idx}"] = p.numpy()
        q = dev.card.queues[0]
        report = {
            "case": "lenet-step",
            "fw": str(args.fw),
            "board": args.board,
            "resident": args.resident,
            "wall_seconds": time.monotonic() - t0,
            "launches": dev.counters["runs"],
            "cycles": dev.counters["cycles"],
            "bytes_in": dev.counters["bytes_in"],
            "bytes_out": dev.counters["bytes_out"],
            "upload_bytes": q.counters["upload_bytes"],
            "sq_tail": q.sq_tail,
            "loss": float(arrays["loss"].item()),
            "arrays": {k: array_digest(v) for k, v in arrays.items()},
            "total_output_bytes": int(
                sum(np.ascontiguousarray(v).nbytes for v in arrays.values())
            ),
        }
    np.savez(
        args.output.with_suffix(".npz"),
        **{k: np.ascontiguousarray(v).view(np.uint8) for k, v in arrays.items()},
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps({k: v for k, v in report.items() if k != "arrays"}),
        flush=True,
    )
    return report


def compare_outputs(left_npz: Path, right_npz: Path) -> dict:
    a = np.load(left_npz)
    b = np.load(right_npz)
    assert set(a.files) == set(b.files), (a.files, b.files)
    diffs = {
        k: int(np.sum(a[k].reshape(-1) != b[k].reshape(-1))) for k in sorted(a.files)
    }
    total_bytes = int(sum(a[k].nbytes for k in a.files))
    return {
        "bit_exact": all(v == 0 for v in diffs.values()),
        "total_bytes": total_bytes,
        "differing_bytes": sum(diffs.values()),
        "per_array_diffs": diffs,
    }


def run_compare(args) -> dict:
    d = args.output.parent
    vit_base = json.loads((d / "vit-base.json").read_text(encoding="utf-8"))
    vit_hoste = json.loads((d / "vit-hoste.json").read_text(encoding="utf-8"))
    list_base_run = json.loads((d / "list-base-run.json").read_text(encoding="utf-8"))
    list_hoste_run = json.loads((d / "list-hoste-run.json").read_text(encoding="utf-8"))
    list_hoste_list = json.loads(
        (d / "list-hoste-list.json").read_text(encoding="utf-8")
    )
    lenet_base_r0 = json.loads((d / "lenet-base-r0.json").read_text(encoding="utf-8"))
    lenet_hoste_r0 = json.loads((d / "lenet-hoste-r0.json").read_text(encoding="utf-8"))
    lenet_base_r1 = json.loads((d / "lenet-base-r1.json").read_text(encoding="utf-8"))
    lenet_hoste_r1 = json.loads((d / "lenet-hoste-r1.json").read_text(encoding="utf-8"))

    summary = {
        "vit_block": {
            "base_cycles": vit_base["cycles"],
            "hoste_cycles": vit_hoste["cycles"],
            "launches": vit_hoste["launches"],
            "parity": compare_outputs(d / "vit-base.npz", d / "vit-hoste.npz"),
        },
        "queue_v4_list": {
            "base_run_cycles": list_base_run["replay_cycles"],
            "hoste_run_cycles": list_hoste_run["replay_cycles"],
            "hoste_list_cycles": list_hoste_list["replay_cycles"],
            "run_sq_entries": list_base_run["sq_entries"],
            "list_sq_entries": list_hoste_list["sq_entries"],
            "recorded_packages": list_hoste_list["recorded_packages"],
            "hoste_run_vs_base_run": compare_outputs(
                d / "list-base-run.npz", d / "list-hoste-run.npz"
            ),
            "hoste_list_vs_base_run": compare_outputs(
                d / "list-base-run.npz", d / "list-hoste-list.npz"
            ),
        },
        "lenet_train_step": {
            "residency_off": {
                "base_cycles": lenet_base_r0["cycles"],
                "hoste_cycles": lenet_hoste_r0["cycles"],
                "base_upload_bytes": lenet_base_r0["upload_bytes"],
                "hoste_upload_bytes": lenet_hoste_r0["upload_bytes"],
                "launches": lenet_hoste_r0["launches"],
                "loss": lenet_hoste_r0["loss"],
                "parity": compare_outputs(
                    d / "lenet-base-r0.npz", d / "lenet-hoste-r0.npz"
                ),
            },
            "residency_on": {
                "base_cycles": lenet_base_r1["cycles"],
                "hoste_cycles": lenet_hoste_r1["cycles"],
                "base_upload_bytes": lenet_base_r1["upload_bytes"],
                "hoste_upload_bytes": lenet_hoste_r1["upload_bytes"],
                "launches": lenet_hoste_r1["launches"],
                "loss": lenet_hoste_r1["loss"],
                "parity": compare_outputs(
                    d / "lenet-base-r1.npz", d / "lenet-hoste-r1.npz"
                ),
            },
            "residency_on_vs_off_hoste": compare_outputs(
                d / "lenet-hoste-r0.npz", d / "lenet-hoste-r1.npz"
            ),
        },
    }
    assert summary["vit_block"]["parity"]["bit_exact"]
    assert summary["queue_v4_list"]["hoste_run_vs_base_run"]["bit_exact"]
    assert summary["queue_v4_list"]["hoste_list_vs_base_run"]["bit_exact"]
    assert summary["lenet_train_step"]["residency_off"]["parity"]["bit_exact"]
    assert summary["lenet_train_step"]["residency_on"]["parity"]["bit_exact"]
    assert summary["lenet_train_step"]["residency_on_vs_off_hoste"]["bit_exact"]
    args.output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)
    return summary


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--case",
        choices=("vit-block", "list", "lenet-step", "compare"),
        required=True,
    )
    ap.add_argument("--fw", type=Path, default=ROOT / "build/fw/kohakutpu_node.elf")
    ap.add_argument("--board", default="vlt_v9t6s_1n")
    ap.add_argument(
        "--build",
        type=Path,
        default=ROOT / "build/v9t6s/vlt_card_v9t6s_1n",
    )
    ap.add_argument("--resident", type=int, choices=(0, 1), default=0)
    ap.add_argument("--lists", action="store_true")
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.case == "vit-block":
        run_vit_block(args)
    elif args.case == "list":
        run_list_path(args)
    elif args.case == "lenet-step":
        run_lenet_step(args)
    else:
        run_compare(args)


if __name__ == "__main__":
    main()
