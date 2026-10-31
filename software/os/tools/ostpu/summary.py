"""Verify bit-exact outputs and summarize OSTPU vs HOSTL-LIST/firmware result files."""

import argparse
import json
import pathlib
import sys

import numpy as np

TOOLS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS / "ostpu"))

from attribution import ring_summary


def load_json(path):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def summarize_results(results, reference=None, check_sensitivity=False):
    if not results:
        raise ValueError("at least one result JSON is required")
    ref_outputs = None
    if reference is not None:
        ref_res = (
            reference["os"]["results"]
            if reference.get("backend") == "compare"
            else reference["results"]
        )
        ref_outputs = [r["outputs"] for r in ref_res]

    entries = []
    for item in results:
        if item.get("backend") == "compare":
            os_epochs = item["os"]["results"]
            fw_epochs = item["firmware"]["results"]
            if [r["outputs"] for r in os_epochs] != [r["outputs"] for r in fw_epochs]:
                raise ValueError(
                    f"OS and firmware outputs differ in {item.get('workload')}"
                )
            if (
                ref_outputs is not None
                and [r["outputs"] for r in os_epochs] != ref_outputs
            ):
                raise ValueError(
                    f"outputs differ from reference in {item.get('workload')}"
                )
            entries.append(
                dict(
                    workload=item.get("workload"),
                    backend="compare",
                    board=item.get("board"),
                    bit_exact=True,
                    os_cold_cycles=item["os"]["cold_cycles"],
                    os_retained_cycles=item["os"]["retained_cycles"],
                    fw_step0_cycles=item["firmware"]["step0_cycles"],
                    fw_step1_cycles=item["firmware"]["step1_cycles"],
                    speedup_cold_vs_fw=item["comparison"]["speedup_cold_vs_fw"],
                    speedup_retained_vs_fw=item["comparison"]["speedup_retained_vs_fw"],
                )
            )
            continue

        epochs = item["results"]
        outputs = [r["outputs"] for r in epochs]
        if ref_outputs is None:
            raise ValueError(
                "--ref is required to verify bit-exactness of non-compare result files"
            )
        if len(outputs) == len(ref_outputs):
            if outputs != ref_outputs:
                raise ValueError(
                    f"outputs differ from reference in {item.get('workload')}"
                )
        else:
            for out in outputs:
                if out != ref_outputs[0]:
                    raise ValueError(
                        f"step output differs from reference in {item.get('workload')}"
                    )

        if check_sensitivity and len(outputs) >= 2:
            arr0 = np.frombuffer(bytes.fromhex(outputs[0][0]), dtype=np.float16)
            arr1 = np.frombuffer(bytes.fromhex(outputs[1][0]), dtype=np.float16)
            if not (np.all(np.isfinite(arr0)) and np.all(np.isfinite(arr1))):
                raise ValueError("non-finite values in output")
            changed = int(np.sum(arr0 != arr1))
            if changed == 0:
                raise ValueError("warm output did not change across distinct inputs")
        else:
            changed = None

        ring = (
            ring_summary(epochs[0]["launch_ring"], is_os=(item.get("backend") == "os"))
            if epochs[0].get("launch_ring")
            else None
        )
        entries.append(
            dict(
                workload=item.get("workload"),
                backend=item.get("backend"),
                board=item.get("board"),
                cycles=[r["cycles"] for r in epochs],
                step_ring=[r.get("step_ring") for r in epochs if r.get("step_ring")],
                launch_ring_summary=ring,
                changed_elements=changed,
            )
        )
    return dict(bit_exact=True, count=len(entries), entries=entries)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("results", nargs="+", type=pathlib.Path)
    p.add_argument("--ref", type=pathlib.Path)
    p.add_argument("--check-sensitivity", action="store_true")
    p.add_argument("--out", type=pathlib.Path)
    a = p.parse_args()
    loaded = [load_json(path) for path in a.results]
    if a.ref is None and any(r.get("backend") != "compare" for r in loaded):
        p.error("--ref is required when summarizing non-compare result files")
    ref = load_json(a.ref) if a.ref else None
    summary = summarize_results(
        loaded, reference=ref, check_sensitivity=a.check_sensitivity
    )
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
