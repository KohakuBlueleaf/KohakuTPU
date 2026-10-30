"""Offline vector/quant instruction census; no simulated cycles are invented.

Run from software/ through hostrun. Byte and lane-op counts are for the named
reduced, aligned kernels, not extrapolations of LeNet or ViT execution.
"""

import argparse
import json
import re
from pathlib import Path

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def census(source, machine, *, mutant=None):
    result = rs._rs.compile(
        source, rs.machine_json(machine), cache_dir="", fetch=True, mutant=mutant
    )
    text = result["l1"]
    return {
        "refused": result["verdict"]["refused"],
        "findings": result["verdict"]["findings"],
        "package_bytes": sum(len(p["bytes"]) for p in result["packages"].values()),
        "static_unpacks": len(re.findall(r"(?m)^\s*vunpk ", text)),
        "static_packs": len(re.findall(r"(?m)^\s*vpack ", text)),
        "static_fe_unpack_waits": text.count("vsync fe on=unpack"),
        "static_fe_pack_waits": text.count("vsync fe on=pack"),
        "static_fe_math_waits": text.count("vsync fe on=math"),
        "static_fills": len(re.findall(r"(?m)^\s*vfill ", text)),
        "static_drains": len(re.findall(r"(?m)^\s*vdrain ", text)),
        "cycles": None,
        "achieved_fraction": None,
        "stall_counters": None,
    }


def traffic(read_bytes, write_bytes, lane_ops, machine):
    facts = json.loads(rs.machine_json(machine))["machine"]
    word = facts["die"]["word_bytes"]
    lanes = facts["vc"]["lanes"]
    # FILL and DRAIN have separate engines; the shared-link bound is also shown.
    return {
        "read_bytes": read_bytes,
        "write_bytes": write_bytes,
        "bytes_moved": read_bytes + write_bytes,
        "math_lane_ops": lane_ops,
        "ideal_engine_cycles": max(
            read_bytes / word, write_bytes / word, lane_ops / lanes
        ),
        "shared_link_cycles": (read_bytes + write_bytes) / word,
    }


def measure():
    machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    records = []
    for checked in (False, True):
        for batch in (1, 2, 4):
            source = (
                "target ktpu.v9\n"
                "fn q.l2(x: f16[64,32] @dram, "
                "q: mx7a[64,32] @dram(gm=16,nk=1))\n"
                f"    quant q <- x finite_batch={batch} overlap=1"
                + ("\n" if checked else " finite=1\n")
            )
            variants = [(False, None)]
            if batch == 4:
                variants.append((False, "l2.packing/no-finite-batch"))
                if checked:
                    variants.extend(
                        [(True, None), (True, "l2.packing/serial-classifier")]
                    )
            for classifier_overlap, mutant in variants:
                selected = source.rstrip() + (
                    " classifier_overlap=1\n" if classifier_overlap else "\n"
                )
                # Checked geometry: clear plus two RUN status folds (14+2 entries).
                status_bytes = 128 if checked else 0
                lane_ops = 5 * 64 * 32 + 3 * 128 if checked else 0
                records.append(
                    dict(
                        kernel="quant-checked" if checked else "quant-finite",
                        shape=[64, 32],
                        finite_batch=batch,
                        classifier_overlap=classifier_overlap,
                        mutant=mutant,
                        **traffic(4096, 2048 + status_bytes, lane_ops, machine),
                        **census(selected, machine, mutant=mutant),
                    )
                )
    for bands in (1, 4):
        kernel, given, _ = V.rows_kernel(
            X.op("max", X.Load(0), 0.0),
            (V.Operand("full", pitch=128),),
            16,
            128,
            V.Operand("full", pitch=128),
            1,
            bands=bands,
        )
        source = json.dumps(kernel.trace(given, machine=K.dsl_machine(machine)).graph)
        records.append(
            dict(
                kernel="rows-relu",
                shape=[16, 128],
                bands=bands,
                **traffic(4096, 4096, 2048, machine),
                **census(source, machine),
            )
        )
    return {
        "board": machine.board,
        "mode": "offline-static",
        "definitions": {
            "cycles": "null: no model or card executed",
            "static": "instruction occurrences in emitted images, not dynamic counts",
            "math_lane_ops": "issued ALU lane slots, including checked-quant bookkeeping",
            "ideal_engine_cycles": "max(read bytes/word, write bytes/word, math lane ops/lanes); optimistic, excludes MX7 pack service latency",
            "shared_link_cycles": "aggregate data bytes/word; use only for a shared read/write bottleneck",
        },
        "records": records,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    text = json.dumps(measure(), indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
