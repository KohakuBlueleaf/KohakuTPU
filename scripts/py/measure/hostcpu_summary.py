"""Exclusive host layer attribution from hseam profiles (never sum inclusive rows)."""

import argparse
import json
from collections import defaultdict
from pathlib import Path


def layer(row):
    file, fn = row["file"].replace("\\", "/"), row["function"]
    if "package_admit" in fn:
        return "Rust admission"
    if "_rs.bind" in fn or fn == "<built-in method kohakutpu.compiler._rs.bind>":
        return "Rust bind"
    if "Cores" in fn:
        return "Rust cores"
    if "/tinygrad/" in file:
        return "tinygrad scheduling/rewrite/engine"
    if "/ktpugrad/" in file:
        return "ktpugrad planning"
    if "/runtime/forms.py" in file or "/runtime/generated.py" in file:
        return "forms/generated planning"
    if "/runtime/" in file and "/driver/" not in file:
        return "runtime submit/MMU/arena/library"
    if "/driver/" in file or "hseam_counting.py" in file:
        return "queue/transport double"
    return "other/Python/NumPy"


def summarize(path):
    doc = json.loads(path.read_text(encoding="utf-8"))
    rows = doc.get("steps", [])
    warm = [r for r in rows[2:] if not r["profiled"]]
    profiled = next((r for r in rows if r["profiled"]), None)
    own = defaultdict(float)
    if profiled:
        for row in doc["profile"]:
            if (
                row["file"] == "~"
                and row.get("callers")
                and layer(row) == "other/Python/NumPy"
            ):
                for caller in row["callers"]:
                    own[layer(caller)] += (
                        1000 * caller["exclusive_seconds"] / profiled["launches"]
                    )
            else:
                own[layer(row)] += (
                    1000 * row["exclusive_seconds"] / profiled["launches"]
                )
    events = [e for e in doc.get("events", []) if e["step"] == len(rows) - 1]
    phases = [one["phases"] for e in events for one in e["nodes"].values()]
    return {
        "file": str(path),
        "status": doc["status"],
        "launches": rows[-1]["launches"] if rows else 0,
        "warm_cpu_ms_per_launch": (
            1000
            * sum(r["host_cpu_seconds"] for r in warm)
            / sum(r["launches"] for r in warm)
            if warm
            else None
        ),
        "warm_wall_ms_per_launch": (
            1000 * sum(r["seconds"] for r in warm) / sum(r["launches"] for r in warm)
            if warm
            else None
        ),
        "profile_exclusive_ms_per_launch": dict(own),
        "profile_timer": doc.get("profile_timer", "perf_counter (wall)"),
        "profile_cpu_scaled_ms_per_launch": (
            {
                k: v / sum(own.values()) * profiled["host_cpu_ms_per_launch"]
                for k, v in own.items()
            }
            if profiled and sum(own.values())
            else {}
        ),
        "traffic_calls": rows[-1].get("transport_calls") if rows else None,
        "compile_calls": doc.get("counters", {}).get("compile_calls"),
        "package_records": len(phases),
        "raw_idle_cycles": sum(p.get("idle", 0) for p in phases),
        "raw_total_cycles": sum(e.get("cycles", 0) for e in events),
        "overlap_interpretation": "not measurable: offline/counting has no autonomous card clock",
        "focus_functions": (
            [
                {
                    "file": row["file"],
                    "function": row["function"],
                    "calls": row["calls"],
                    "exclusive_ms_per_launch": row["exclusive_seconds"]
                    * 1000
                    / profiled["launches"],
                    "inclusive_ms_per_launch": row["inclusive_seconds"]
                    * 1000
                    / profiled["launches"],
                }
                for row in doc.get("profile", [])
                if row["function"]
                in (
                    "_vector",
                    "_rows",
                    "_bind",
                    "launch",
                    "containing",
                    "resolve",
                    "translations_for",
                    "kernel",
                    "submit",
                    "unified_rewrite",
                    "create_linear_with_vars",
                )
                or "Cores" in row["function"]
            ]
            if profiled
            else []
        ),
        "top_functions": [
            {k: row[k] for k in ("file", "function", "calls", "exclusive_seconds")}
            for row in doc.get("profile", [])[:15]
        ],
    }


def tables(summaries):
    configs = ("off", "admission", "plan", "arena", "submit", "on")
    grouped = defaultdict(dict)
    for row in summaries:
        _, work, backend, config = Path(row["file"]).stem.split("-")
        grouped[(work, backend)][config] = row
    text = [
        "# HOSTCPU measured tables",
        "",
        "## Unprofiled warm host CPU ms/launch",
        "",
        "| Workload/backend | " + " | ".join(configs) + " |",
        "|---|" + "---:|" * len(configs),
    ]
    for key, group in sorted(grouped.items()):
        text.append(
            "| "
            + "/".join(key)
            + " | "
            + " | ".join(f"{group[c]['warm_cpu_ms_per_launch']:.3f}" for c in configs)
            + " |"
        )
    for key, group in sorted(grouped.items()):
        text.extend(
            [
                "",
                "## " + "/".join(key) + " — profiled exclusive elapsed ms/launch",
                "",
                "| Layer | " + " | ".join(configs) + " |",
                "|---|" + "---:|" * len(configs),
            ]
        )
        layers = sorted(
            set().union(*(r["profile_exclusive_ms_per_launch"] for r in group.values()))
        )
        for name in layers:
            text.append(
                "| "
                + name
                + " | "
                + " | ".join(
                    f"{group[c]['profile_exclusive_ms_per_launch'].get(name, 0):.4f}"
                    for c in configs
                )
                + " |"
            )
        text.append(
            "| Profiled elapsed total | "
            + " | ".join(
                f"{sum(group[c]['profile_exclusive_ms_per_launch'].values()):.4f}"
                for c in configs
            )
            + " |"
        )
        before, after = group["off"], group["on"]
        delta = before["warm_cpu_ms_per_launch"] - after["warm_cpu_ms_per_launch"]
        text.extend(
            [
                "",
                f"Warm CPU delta {delta:.4f} ms/launch ({100 * delta / before['warm_cpu_ms_per_launch']:.2f}%).",
                (
                    f"Launches {after['launches']}; traffic calls {before['traffic_calls']} -> {after['traffic_calls']}; "
                    f"package records {after['package_records']}; raw idle/total cycles "
                    f"{after['raw_idle_cycles']}/{after['raw_total_cycles']}."
                ),
            ]
        )
    return "\n".join(text) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tables", type=Path)
    args = parser.parse_args()
    summaries = [summarize(p) for p in args.paths]
    args.output.write_text(json.dumps(summaries, indent=2) + "\n", encoding="utf-8")
    if args.tables:
        args.tables.write_text(tables(summaries), encoding="utf-8")
    for row in summaries:
        print(row["file"], row["warm_cpu_ms_per_launch"])
        print(json.dumps(row["profile_exclusive_ms_per_launch"], sort_keys=True))


if __name__ == "__main__":
    main()
