"""Capture isolated workload kernels and their live input/alias maps via prebuild.

This is not an end-to-end numerical run: tensors produced by offline computation
are placeholders. Initial weights, index tables and constants retain their bytes.
"""

import argparse
import json
import math
import pickle
import runpy
from collections import Counter
from pathlib import Path

from kohakutpu.application.runtime import device as D
from kohakutpu.stack import rs

ROOT = Path(__file__).resolve().parents[3]
ITEM = {"f16": 2, "f32": 4, "mx7a": 1, "mx7b": 1}


def capture(args):
    prebuild = runpy.run_path(str(ROOT / "scripts/py/templates/prebuild.py"))
    dev = D.Device(kind="offline", board="vlt_v9t6s_1n", nodes=1)
    held, order = {}, []

    def observe(key, source, binds, kind, phase, capture_pass):
        if args.model == "lenet" and phase != "jit_step2":
            return
        if order and order[-1][0] != capture_pass:
            held.clear()
            order.clear()
        order.append((capture_pass, key))
        if key in held:
            return
        graph = json.loads(source)
        allocations, views = {}, {}
        for port in graph["ports"]:
            name, spec = port["name"], port["spec"]
            entry, _ = dev.mmu.resolve(binds[name])
            size = math.prod(spec["shape"]) * ITEM[spec["dtype"]]
            base = entry.virtual
            if base not in allocations:
                allocations[base] = dict(
                    data=dev.read(base, entry.extent), extent=entry.extent
                )
            offset = binds[name] - base
            allocations[base]["extent"] = max(
                allocations[base]["extent"], offset + size
            )
            views[name] = dict(
                allocation=base, offset=offset, nbytes=size, role=port["role"]
            )
        held[key] = dict(
            key=key,
            label=graph["name"],
            source=source,
            graph=graph,
            kind=kind,
            allocations=allocations,
            views=views,
        )

    with D.attached(dev):
        prebuild["capture"](
            dev,
            8 if args.model == "lenet" else 1,
            args.model != "lenet",
            args.model,
            observe_kernel=observe,
        )
    dev.close()
    counts = Counter(key for _, key in order)
    report, cases = [], []
    for i, (key, case) in enumerate(held.items()):
        case["multiplicity"] = counts[key]
        try:
            result = rs._rs.compile(
                case["source"], rs.machine_json(dev.machine), cache_dir="", fetch=True
            )
            case["compiled"] = result
            case["after"] = result
            case["changed"] = False
            if result["verdict"]["refused"] or result["bound"] is not None:
                report.append(
                    dict(
                        key=key,
                        name=case["label"],
                        status="refused",
                        verdict=result["verdict"],
                        bound=result["bound"],
                    )
                )
            else:
                cases.append(case)
                report.append(
                    dict(
                        key=key,
                        name=case["label"],
                        status="compiled",
                        kind=case["kind"],
                        multiplicity=counts[key],
                        package_bytes=sum(
                            len(p["bytes"]) for p in result["packages"].values()
                        ),
                    )
                )
        except ValueError as error:
            report.append(
                dict(key=key, name=case["label"], status="refused", error=str(error))
            )
        print(json.dumps(dict(i=i, **report[-1])), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(
        pickle.dumps(
            dict(machine=dev.machine.machine_json, cases=cases, model=args.model)
        )
    )
    args.output.with_suffix(".json").write_text(
        json.dumps(
            dict(
                model=args.model, classes=len(held), launches=len(order), report=report
            ),
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            dict(
                model=args.model,
                classes=len(held),
                launches=len(order),
                compiled=len(cases),
            )
        ),
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=("lenet", "vit-tiny"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    capture(parser.parse_args())


if __name__ == "__main__":
    main()
