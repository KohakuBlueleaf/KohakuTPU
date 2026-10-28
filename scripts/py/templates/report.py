"""Summarize the persisted offline derivation ledger and check its artifacts."""

import argparse
import json
import pathlib
from collections import Counter


def summarize(results):
    doc = json.loads(results.read_text(encoding="utf-8"))
    nontrivial, patched = Counter(), Counter()
    for artifact in doc["artifacts"]:
        family = artifact["generator"]
        nontrivial[family] += any(
            b["lo"] != b["hi"] for b in artifact["bounds"].values()
        )
        has_sites = False
        for stem in artifact["meshes"].values():
            root = results.parent / "experimental-templates"
            if not (root / f"{stem}.bin").is_file():
                raise FileNotFoundError(stem)
            table = json.loads((root / f"{stem}.json").read_text(encoding="utf-8"))
            for name, bound in artifact["bounds"].items():
                if table["parameters"].get(name) != bound:
                    raise ValueError(f"bounds differ: {stem}: {name}")
            if set(table["parameters"]) != set(artifact["bounds"]) | set(
                artifact.get("values", {})
            ):
                raise ValueError(f"unexpected or missing derived parameter: {stem}")
            has_sites |= bool(table["sites"])
        patched[family] += has_sites
    totals = Counter()
    lines = [
        "| Generator | Launches | Initial classes | Templates | Non-singleton boxes | Patched templates | Proven launches | Structure splits | Choice splits | Other splits | Non-affine refusals |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    timing = [
        "| Generator | Direct seconds | Derivation seconds | Direct s/launch | Derivation s/template |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, r in doc["generators"].items():
        values = [
            r["launches"],
            r["classes"],
            r["experimental_templates"],
            nontrivial[name],
            patched[name],
            r["proven"],
            r["structure_splits"],
            r.get("choice_splits", 0),
            r["other_splits"],
            len(r["non_affine_fields"]),
        ]
        lines.append("| " + name + " | " + " | ".join(map(str, values)) + " |")
        for i, v in enumerate(values):
            totals[i] += v
        direct, derive = r["direct_seconds"], r["derivation_compile_seconds"]
        d = f"{direct / r['launches']:.6f}" if r["launches"] else "—"
        t = (
            f"{derive / r['experimental_templates']:.6f}"
            if r["experimental_templates"]
            else "—"
        )
        timing.append(f"| {name} | {direct:.6f} | {derive:.6f} | {d} | {t} |")
    lines.append(
        "| **Total** | " + " | ".join(str(totals[i]) for i in range(10)) + " |"
    )
    direct = sum(r["direct_seconds"] for r in doc["generators"].values())
    derive = sum(r["derivation_compile_seconds"] for r in doc["generators"].values())
    timing.append(
        f"| **Total** | {direct:.6f} | {derive:.6f} | {direct / totals[0]:.6f} | {derive / totals[2]:.6f} |"
    )
    proven = [launch for a in doc["artifacts"] for launch in a["launches"]]
    if len(proven) != len(set(proven)) or len(proven) != totals[5]:
        raise ValueError("proof ledger is not one-to-one with launches")
    print("\n".join(lines))
    print("\n".join(timing))
    print(
        f"artifact index verified: {len(doc['artifacts'])} templates, {len(proven)} unique launch witnesses; excluded lanes {doc['excluded_lanes']}"
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("results", type=pathlib.Path)
    summarize(ap.parse_args().results)


if __name__ == "__main__":
    main()
