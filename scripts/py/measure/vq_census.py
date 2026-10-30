"""Count executed L1 vector work for constant-trip compiled packages.

Expands image calls, loop and skipz trip counts, descriptor word extents and
VL. This is an instruction census, not a timing simulator. Counter totals are
checked against VRES from the unchanged Verilated model before publishing.
"""

import argparse
import json
import math
import pickle
import re
from collections import Counter
from pathlib import Path

MATH = frozenset(
    [
        "vmov",
        "vadd",
        "vmul",
        "vfma",
        "vfnma",
        "vabs",
        "vmax",
        "vmin",
        "vsel",
        "vcmpeq",
        "vcmplt",
        "vcmpgt",
        "vinv",
        "vrsqrt",
        "vexp2",
        "vexp2d",
        "vlog2",
        "vneg",
    ]
)


def tree(text):
    root = []
    stack = [(-1, root)]
    for line in text.splitlines():
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        while stack[-1][0] >= indent:
            stack.pop()
        children = []
        stack[-1][1].append((line.strip(), children))
        stack.append((indent, children))
    return root


def census(text):
    nodes = tree(text)
    images = {}
    for line, children in nodes:
        if line.startswith("image "):
            name, params = re.fullmatch(r"image ([^(]+)\((.*)\)", line).groups()
            images[name] = (
                [x.strip() for x in params.split(",") if x.strip()],
                children,
            )
    cache = {}

    def number(s, env):
        value = env[s] if s in env else float(s)
        return int(value) if math.isfinite(value) else value

    def execute(nodes, env, desc, vector=False):
        result = Counter()
        for line, children in nodes:
            op = line.split()[0]
            if op == "seti":
                reg, value = re.fullmatch(
                    r"seti (\w+)\s*=\s*(\S+)(?: classed=1)?", line
                ).groups()
                env[reg] = number(value, env)
            elif op == "desc":
                name, spec = re.fullmatch(r"desc (a\d+)=.* walk=(.*)", line).groups()
                pairs = re.findall(r"\(([^(),]+),\s*([^(),]+)\)", spec)
                desc[name] = math.prod(number(count, env) for _, count in pairs)
            elif op in ("loop", "skipz"):
                count = number(line.split()[1], env)
                factor = count if op == "loop" else int(count != 0)
                if factor:
                    got = execute(children, env.copy(), desc.copy(), vector)
                    result.update({k: v * factor for k, v in got.items()})
                continue
            elif op == "run":
                name, arguments = re.fullmatch(r"run ([^(]+)\((.*)\)", line).groups()
                params, body = images[name]
                args = [x.strip() for x in arguments.split(",") if x.strip()]
                values = {
                    p: (int(a) if re.fullmatch(r"-?\d+", a) else 0)
                    for p, a in zip(params, args, strict=True)
                }
                key = (name, tuple(values.items()), tuple(desc.items()))
                if key not in cache:
                    cache[key] = execute(body, values, desc.copy(), True)
                result.update(cache[key])
                result["image_runs"] += 1
            elif op in ("vfill", "vdrain"):
                name = re.search(r"\ba\d+\b", line)[0]
                result["fill_words" if op == "vfill" else "drain_words"] += desc[name]
                result["fill_requests" if op == "vfill" else "drain_requests"] += 1
            elif op in MATH:
                vl = re.search(r"\bvl=(\d+)", line)
                n = int(vl[1]) if vl else 128
                result["lane_slots"] += ((n + 15) // 16) * 16
                result["math_instructions"] += 1
            elif op == "vsync":
                result["fence_" + line.split()[1]] += 1
                result["fence_" + "_".join(line.split()[1:3])] += 1
            elif op in ("copy", "gt4", "fill", "gather", "scatter") and not vector:
                # Mover transactions use w(base,(count,stride),...), not VC descriptors.
                if "w(" in line:
                    words = math.prod(
                        int(n)
                        for n in re.findall(
                            r"\((\d+),\s*-?\d+\)",
                            line.split(" -> ")[0 if op != "gather" else 1],
                        )
                    )
                    if op in ("gather", "scatter"):
                        words = int(re.search(r"\bn=(\d+)", line)[1]) * int(
                            re.search(r"\bwords=(\d+)", line)[1]
                        )
                        result["index_bytes"] += (
                            int(re.search(r"\bn=(\d+)", line)[1]) * 4
                        )
                    result["mover_write_bytes"] += words * 32
                    if op != "fill":
                        result["mover_read_bytes"] += words * 32
            if children:
                result.update(execute(children, env, desc, vector))
        return result

    totals = Counter()
    for line, children in nodes:
        if line.startswith("fn "):
            totals.update(execute(children, {}, {}))
    for field in ("fill_words", "drain_words", "lane_slots"):
        totals[field] += 0
    totals["vector_bytes"] = (totals["fill_words"] + totals["drain_words"]) * 32
    totals["engine_floor_cycles"] = max(
        totals["fill_words"], totals["drain_words"], totals["lane_slots"] // 16
    )
    return dict(totals)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--measurement", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    bundle = pickle.loads(args.artifact.read_bytes())
    measured = json.loads(args.measurement.read_text(encoding="utf-8"))
    seen = {r["key"]: r for r in measured["records"]}
    rows, aggregate = [], Counter()
    for case in bundle["cases"]:
        r = dict(seen[case["key"]])
        for variant, cycle in (("compiled", "before"), ("after", "after")):
            counts = census(case[variant]["l1"])
            r[cycle + "_work"] = counts
            r[cycle + "_efficiency"] = counts["engine_floor_cycles"] / r[cycle]
            if variant == "compiled" or case["changed"]:
                aggregate.update(counts)
        rows.append(r)
    counters = next(iter(measured["counters"].values()))["VRES"]
    checks = {
        key: {
            "census": aggregate[key],
            "model": counters["lanes" if key == "lane_slots" else key],
        }
        for key in ("fill_words", "drain_words", "lane_slots")
    }
    args.output.write_text(
        json.dumps({"rows": rows, "counter_checks": checks}, indent=2), encoding="utf-8"
    )
    print(json.dumps(checks))
    assert all(p["census"] == p["model"] for p in checks.values()), checks


if __name__ == "__main__":
    main()
