"""One captured original relayout versus certified library classes on v9t6s."""

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import ktpu_pytest_lock
import numpy as np
from kohakutpu.application.kernels import lanes as L
from kohakutpu.application.kernels.classes import lanes as C
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import forms
from kohakutpu.application.runtime.packages import Prebuilt
from kohakutpu.application.runtime.template_store import TemplateStore
from kohakutpu.stack import rs
from lanes_classes import parameters, rotation_recipes

ROOT = Path(__file__).resolve().parents[3]


def decode(value):
    if isinstance(value, list):
        return tuple(decode(x) for x in value)
    if isinstance(value, dict):
        if value.get("type") == "ndarray":
            return np.array(value["values"], dtype=value["dtype"])
        if value.get("type") in ("Plan", "Class"):
            return getattr(L, value["type"])(
                **{k: decode(v) for k, v in value["fields"].items()}
            )
        return {k: decode(v) for k, v in value.items()}
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("launch", type=int)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "build/lib/lanes-values")
    args = parser.parse_args()
    doc = json.loads(args.capture.read_text(encoding="utf-8"))
    launches = [
        (i, doc["records"][entry["record"]])
        for i, entry in enumerate(doc["launches"])
        if doc["records"][entry["record"]]["generator"] == "lanes"
    ]
    launch, record = launches[args.launch]
    plan = decode(record["arguments"])["pl"]
    ktpu_pytest_lock.pytest_configure(SimpleNamespace())
    dev = None
    started = time.perf_counter()
    result = {
        "command": "python " + " ".join(sys.argv),
        "ordinal": args.launch,
        "launch": launch,
        "class_instances": len(plan.classes),
    }
    try:
        dev = D.attach(
            D.Device(
                kind="sim",
                board="vlt_v9t6s_1n",
                build=ROOT / "build/v9t6s/vlt_card_v9t6s_1n",
                queued=False,
            )
        )
        dev.card.alarm = 500
        machine = rs.machine_json(dev.machine)
        store = TemplateStore(ROOT / "build/templates", machine)
        data = (
            np.random.default_rng(launch)
            .uniform(-1, 1, plan.src_words * 16)
            .astype(np.float16)
        )
        x, y = dev.alloc(max(65536, plan.src_words) * 32), dev.alloc(
            max(65536, plan.dst_words) * 32
        )
        dev.write(x, data.tobytes())
        dev.write(y, bytes(plan.dst_words * 32))
        kernel, given = L.relayout_kernel(plan, dev.machine.count("VC"))
        forms.launch(
            dev,
            kernel,
            given,
            {
                "src": x,
                "dst": y,
                "index": forms.table(dev, plan.index, np.float32),
                "mask": forms.table(dev, plan.masks),
            },
        )
        expected = dev.read(y, plan.dst_words * 32)
        direct_cycles = dev.counters["cycles"]
        dev.write(y, bytes(plan.dst_words * 32))
        # Offline templates use the mailbox transport, not the fetch-port layout.
        dev.card.fetch = None
        before = dev.counters["compile_calls"]
        library_cycles = 0
        for cls, index, masks in C.tables(plan):
            directory = args.artifacts / f"{len(cls.taps)}-{cls.keep}"
            source = (directory / "source.json").read_text(encoding="utf-8")
            table = (directory / "table.json").read_text(encoding="utf-8")
            names = json.loads((directory / "params.json").read_text(encoding="utf-8"))
            package, table = store.load(source, table)
            buffers = rs._rs.package_read(package, machine)["buffers"]
            handles, owned = [], []
            for name, buffer in zip(names, buffers, strict=True):
                if name in ("src", "dst"):
                    handle = x if name == "src" else y
                else:
                    handle = dev.alloc(buffer["size"])
                    owned.append(handle)
                    if name == "index":
                        dev.write(handle, index.tobytes())
                    if name == "mask":
                        dev.write(handle, masks.tobytes())
                handles.append(handle)
            shape = parameters({"words": cls.words, "taps": cls.taps})
            params = json.loads(
                rs._rs.template_values(
                    json.dumps(rotation_recipes(len(cls.taps))), json.dumps(shape)
                )
            )
            bound = store.bind_handles(
                source, table, params, handles, dev.mmu.translations()
            )
            run = dev.card.run({0: Prebuilt(bound, (), machine, 0, (), False, None)})
            library_cycles += run["cycles"]
            for handle in owned:
                dev.free(handle)
        actual = dev.read(y, plan.dst_words * 32)
        assert actual == expected, "original relayout and bound class values differ"
        assert dev.counters["compile_calls"] == before
        result.update(
            {
                "value_equal": True,
                "output_sha256": hashlib.sha256(actual).hexdigest(),
                "direct_cycles": direct_cycles,
                "library_cycles": library_cycles,
                "library_compile_calls": 0,
                "seconds": time.perf_counter() - started,
            }
        )
        assert result["seconds"] < 600
    finally:
        if dev is not None:
            dev.close()
        ktpu_pytest_lock.pytest_unconfigure(SimpleNamespace())
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / f"{args.launch}.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
