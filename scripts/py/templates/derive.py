"""Offline differential experiment on a saved generator capture (no runtime install).

Image search and retile choices replay conservatively from the representative.
The --unpinned control measures ordinary direct compilation at sample points.
"""

import argparse
import copy
import json
import math
import pathlib
import time
from collections import Counter, defaultdict

from capture import GENERATORS
from kohakutpu.application.kernels import expr, matmul, vector
from kohakutpu.application.kernels.classes import values as launch_values
from kohakutpu.application.kernels.classes.policy import ShapeClass
from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.language import machine as MF
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs

ORDER = (
    "rows",
    "flat",
    "mm",
    "quant",
    "walk",
    "fill",
    "transpose",
    "copy",
    "reverse",
)
TYPES = {
    c.__name__: c
    for c in (expr.Load, expr.Const, expr.Op, expr.Reduce, vector.Operand, matmul.Mm)
}
FIXED = {
    "e",
    "cores",
    "clusters",
    "bands",
    "scalars",
    "g",
    "nk",
    "tm",
    "tn",
    "quantum",
    "ew",
}


def decode(v, memo=None):
    memo = {} if memo is None else memo
    if isinstance(v, list):
        return tuple(decode(x, memo) for x in v)
    if isinstance(v, dict):
        if "ref" in v:
            return memo[v["ref"]]
        if "type" in v:
            result = TYPES[v["type"]](
                **{k: decode(x, memo) for k, x in v["fields"].items()}
            )
            if "id" in v:
                memo[v["id"]] = result
            return result
        return {k: decode(x, memo) for k, x in v.items()}
    return v


def canonical(v):
    return json.dumps(v, sort_keys=True, separators=(",", ":"))


def kinds(v):
    return [(x["fields"]["kind"], x["fields"]["dtype"]) for x in v]


def class_key(record, policy, kblock, max_block):
    family, a = record["generator"], record["arguments"]
    if family == "rows":
        decoded = decode(a)
        quantum = (
            vector.STAT_ROWS
            if vector.holds_stat(decoded["operands"], decoded["out"].kind)
            else 4
        )
        plan = vector.plan(
            decoded["operands"],
            decoded["out"].kind,
            a["R"],
            a["W"],
            bands=a["bands"],
            quantum=quantum,
        )
        key = [
            a["e"],
            kinds(a["operands"]),
            kinds([a["out"]]),
            rs._rs.template_width(a["W"], kblock, max_block, policy),
            record.get("class_info", {}).get("trip_rows", plan.rows) // 4,
        ]
    elif family == "flat":
        key = [a["e"], a["dtypes"], a["out_dtype"], a["scalars"]]
    elif family == "mm":
        mm = a["mm"]["fields"]
        key = [
            a["e"],
            kinds(a["extras"]),
            kinds([a["out"]]),
            [mm[n] for n in ("tm", "tn", "nk", "quantum", "shared")],
            mm["bt"] > 1,
        ]
    elif family == "quant":
        src = a["src"]["fields"]
        direct = (
            a["rows"] == a["rp"]
            and a["cols"] == a["cp"]
            and src["pitch"] == a["cp"]
            and src["offset"] == 0
        )
        key = [a["side"], a["g"], a["nk"], a["batches"] > 1, direct]
    elif family in ("walk", "reverse"):
        key = [len(a["levels"]), len(a.get("dlevels", []))]
    elif family == "fill":
        key = [len(a["dlevels"]), a["ew"]]
    else:
        key = []
    return canonical([family, key])


def parameters(
    args,
    *,
    aligned_rows=False,
    mm_kblock=None,
    quant_kblock=None,
    class_walk=False,
    class_transpose=False,
):
    out = {}
    if class_walk and "levels" in args:
        matched = [x[0] for x in args["levels"]] == [x[0] for x in args["dlevels"]]
        if not matched and len(args["dlevels"]) != 1:
            raise ValueError(
                "walk coordinate box requires matched axes or a linear destination"
            )
        out["walk/matched"] = int(matched)
        for name, base, extent in (
            ("levels", "at", "src_bytes"),
            ("dlevels", "dat", "dst_bytes"),
        ):
            for i, (count, stride) in enumerate(args[name]):
                if name == "levels":
                    out[f"{name}/{i}/0"] = count
                out[f"{name}/{i}/1:units32"] = stride // 32
            out[base + ":units32"] = args[base] // 32
            needed = args[base] + sum((n - 1) * max(s, 0) for n, s in args[name]) + 32
            out[f"walk/{extent}_tail"] = -(-args[extent] // 32) - needed // 32
        return out
    if quant_kblock is not None and "side" in args and "rp" in args:
        src = args["src"]["fields"]
        direct = (
            args["rows"] == args["rp"]
            and args["cols"] == args["cp"]
            and src["pitch"] == args["cp"]
            and src["offset"] == 0
        )
        tile, chunk = args["g"] * 4, args["nk"] * quant_kblock
        row_unit, col_unit = (tile, chunk) if direct else (1, 16)
        out = {
            "batches": args["batches"],
            f"quant/rows:units{row_unit}": args["rows"] // row_unit,
            f"quant/cols:units{col_unit}": args["cols"] // col_unit,
            f"quant/rpad:units{tile}": args["rp"] // tile - -(-args["rows"] // tile),
            f"quant/cpad:units{chunk}": args["cp"] // chunk - -(-args["cols"] // chunk),
            "quant/pitch_extra:units16": (src["pitch"] - args["cols"]) // 16,
            "quant/offset:units16": src["offset"] // 16,
        }
        return out

    def walk(v, path):
        if type(v) is int:
            name = "/".join(map(str, path))
            if (
                aligned_rows
                and "W" in args
                and (path == ("W",) or path[-1] == "pitch")
                and v % 32 == 0
            ):
                out[name + ":columns32"] = v // 32
            elif class_transpose and path in (("A",), ("B",)):
                out[name + ":units16"] = v // 16
            elif (
                mm_kblock is not None
                and "mm" in args
                and path
                in (
                    ("mm", "fields", "mp"),
                    ("mm", "fields", "n"),
                    ("mm", "fields", "kp"),
                )
            ):
                mm = args["mm"]["fields"]
                quantum = {"mp": mm["tm"], "n": mm["tn"], "kp": mm["nk"] * mm_kblock}[
                    path[-1]
                ]
                out[name + f":units{quantum}"] = v // quantum
            elif path == ("nbytes",):
                out[name + ":words32"] = -(-v // 32)
            else:
                out[name] = v
        elif isinstance(v, dict):
            for k, x in v.items():
                if k not in FIXED:
                    walk(x, (*path, k))
        elif isinstance(v, list):
            for k, x in enumerate(v):
                walk(x, (*path, k))

    walk(args, ())
    return out


def with_parameters(args, params):
    result = copy.deepcopy(args)
    qvalues, qunits = {}, {}
    for path, value in params.items():
        if path.startswith("walk/"):
            continue
        if path.startswith("quant/"):
            name, unit = path.removeprefix("quant/").split(":units")
            qvalues[name], qunits[name] = value * int(unit), int(unit)
            continue
        if path.endswith(":columns32"):
            path = path.removesuffix(":columns32")
            value *= 32
        if path.endswith(":words32"):
            path = path.removesuffix(":words32")
            value *= 32
        if ":units" in path:
            path, quantum = path.split(":units")
            value *= int(quantum)
        parts = path.split("/")
        node = result
        for k in parts[:-1]:
            node = node[int(k)] if isinstance(node, list) else node[k]
        node[int(parts[-1]) if isinstance(node, list) else parts[-1]] = value
    if "walk/matched" in params:
        source = result["levels"]
        if params["walk/matched"]:
            for src, dst in zip(source, result["dlevels"]):
                dst[0] = src[0]
        else:
            result["dlevels"][0][0] = math.prod(n for n, _ in source)
        for name, base, extent in (
            ("levels", "at", "src_bytes"),
            ("dlevels", "dat", "dst_bytes"),
        ):
            needed = (
                result[base] + sum((n - 1) * max(s, 0) for n, s in result[name]) + 32
            )
            result[extent] = needed + params[f"walk/{extent}_tail"] * 32
    if qvalues:
        result["rows"], result["cols"] = qvalues["rows"], qvalues["cols"]
        result["rp"] = (
            -(-qvalues["rows"] // qunits["rpad"]) * qunits["rpad"] + qvalues["rpad"]
        )
        result["cp"] = (
            -(-qvalues["cols"] // qunits["cpad"]) * qunits["cpad"] + qvalues["cpad"]
        )
        result["src"]["fields"]["pitch"] = qvalues["cols"] + qvalues["pitch_extra"]
        result["src"]["fields"]["offset"] = qvalues["offset"]
    return result


def skeleton(args):
    return canonical(with_parameters(args, dict.fromkeys(parameters(args), 0)))


def compile_graph(graph, machine):
    graph = dict(graph, name="template")
    start = time.perf_counter()
    result = rs._rs.compile(
        json.dumps(graph), machine, name="template", cache_dir="", fetch=True
    )
    elapsed = time.perf_counter() - start
    if result["bound"] is not None or not result["packages"]:
        raise ValueError(f"non-relocatable: {result['bound']}")
    if result["verdict"]["refused"]:
        raise ValueError(f"verifier refusal: {result['verdict']}")
    return result, elapsed


def run(
    capture,
    output,
    policy,
    max_block,
    unpinned=False,
    class_rows=None,
    class_flat=None,
    class_mm=None,
    class_quant=None,
    class_walk=None,
    class_copy=None,
    class_transpose=None,
):
    doc = json.loads(capture.read_text(encoding="utf-8"))
    machine = doc["machine_json"]
    m = json.loads(machine)["machine"]
    MF.load(doc["board"])
    target = OfflineCard(doc["board"]).machine
    if json.loads(rs.machine_json(target)) != json.loads(machine):
        raise ValueError("capture machine differs from this offline board")
    # Generated graph traces use the captured board's overlay, not a device.
    dsl_machine = K.dsl_machine(target)
    kblock = m["mg"]["kblock"]
    records = doc["records"]
    selected = {
        family
        for family, mode in (
            ("rows", class_rows),
            ("flat", class_flat),
            ("mm", class_mm),
            ("quant", class_quant),
            ("walk", class_walk),
            ("copy", class_copy),
            ("transpose", class_transpose),
        )
        if mode
    }
    shape_class = ShapeClass(
        kblock,
        max_block,
        policy,
        class_rows
        or class_flat
        or class_mm
        or class_quant
        or class_walk
        or class_copy
        or class_transpose
        or "all",
    )
    factories = {family: getattr(module, name) for module, name, family in GENERATORS}
    trace_errors = {}
    started = time.perf_counter()
    if selected:
        for index, record in enumerate(records):
            if record["generator"] in selected:
                try:
                    kernel, given, *_ = factories[record["generator"]](
                        **decode(record["arguments"]), shape_class=shape_class
                    )
                    record["graph"] = kernel.trace(given, machine=dsl_machine).graph
                    record["class_info"] = kernel.class_info
                except (ValueError, RuntimeError) as error:
                    trace_errors[index] = str(error)
    groups = defaultdict(list)
    counts = Counter(records[x["record"]]["generator"] for x in doc["launches"])
    for launch, x in enumerate(doc["launches"]):
        record = records[x["record"]]
        if record["generator"] != "lanes" and (
            not selected or record["generator"] in selected
        ):
            groups[class_key(record, policy, kblock, max_block)].append(
                (launch, record)
            )
    result = {
        "status": "experimental_unpinned" if unpinned else "search_retile_pinned",
        "shape_class": policy,
        "max_block": max_block,
        "excluded_lanes": counts["lanes"],
        "generators": {},
        "failures": [],
        "artifacts": [],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    artifacts = output.parent / "experimental-templates"
    artifacts.mkdir(exist_ok=True)
    factories = {family: getattr(module, name) for module, name, family in GENERATORS}
    for family in ORDER:
        report = result["generators"][family] = {
            "launches": counts[family],
            "classes": 0,
            "experimental_templates": 0,
            "proven": 0,
            "structure_splits": 0,
            "non_affine_fields": [],
            "other_splits": 0,
            "choice_splits": 0,
            "direct_seconds": 0.0,
            "derivation_compile_seconds": 0.0,
            "derivation_compiles": 0,
        }
        family_groups = [g for g in groups.values() if g[0][1]["generator"] == family]
        report["classes"] = len(family_groups)
        direct = {}
        for group in family_groups:
            for launch, record in group:
                try:
                    if doc["launches"][launch]["record"] in trace_errors:
                        raise ValueError(
                            trace_errors[doc["launches"][launch]["record"]]
                        )
                    direct[launch], seconds = compile_graph(record["graph"], machine)
                    report["direct_seconds"] += seconds
                except (ValueError, RuntimeError) as e:
                    result["failures"].append(
                        {
                            "launch": launch,
                            "generator": family,
                            "phase": "direct",
                            "error": str(e),
                        }
                    )

        def derive(group, direct=direct, report=report, family=family):
            group = [x for x in group if x[0] in direct]
            if not group:
                return
            params = [
                parameters(
                    r["arguments"],
                    aligned_rows=bool(class_rows),
                    mm_kblock=kblock if class_mm else None,
                    quant_kblock=kblock if class_quant else None,
                    class_walk=bool(class_walk),
                    class_transpose=bool(class_transpose),
                )
                for _, r in group
            ]
            if len({skeleton(r["arguments"]) for _, r in group}) > 1:
                partitions = defaultdict(list)
                for x in group:
                    partitions[skeleton(x[1]["arguments"])].append(x)
                report["other_splits"] += len(partitions) - 1
                for part in partitions.values():
                    derive(part)
                return
            bounds = {
                n: {"lo": min(p[n] for p in params), "hi": max(p[n] for p in params)}
                for n in params[0]
            }
            try:
                pts = json.loads(rs._rs.template_points(json.dumps(bounds)))
                recipes = (
                    launch_values.transpose(group[0][1]["class_info"])
                    if family == "transpose" and family in selected
                    else (
                        launch_values.walk(group[0][1]["class_info"])
                        if family == "walk" and family in selected
                        else (
                            {}
                            if family == "copy"
                            else (
                                launch_values.quant(group[0][1]["class_info"])
                                if family == "quant" and family in selected
                                else (
                                    launch_values.mm(group[0][1]["class_info"])
                                    if family == "mm" and family in selected
                                    else (
                                        launch_values.rows(
                                            group[0][1]["class_info"],
                                            aligned_width=True,
                                        )
                                        if family in selected
                                        else {}
                                    )
                                )
                            )
                        )
                    )
                )
                recipes_json = json.dumps(recipes)
                built, sources = [], []
                for p in pts:
                    args = decode(with_parameters(group[0][1]["arguments"], p))
                    kernel, given, *_ = factories[family](
                        **args,
                        **({"shape_class": shape_class} if family in selected else {}),
                        **(
                            {"class_direct": group[0][1]["class_info"]["direct"]}
                            if family == "quant" and family in selected
                            else {}
                        ),
                    )
                    trace = kernel.trace(given, machine=dsl_machine)
                    sources.append(json.dumps(dict(trace.graph, name="template")))
                    if unpinned:
                        got, seconds = compile_graph(trace.graph, machine)
                        report["derivation_compile_seconds"] += seconds
                        report["derivation_compiles"] += 1
                        built.append(got)
                if not unpinned:
                    representative = json.dumps(
                        dict(group[0][1]["graph"], name="template")
                    )
                    start = time.perf_counter()
                    try:
                        built = rs.template_compile(representative, sources, machine)
                        report["derivation_compiles"] += len(built) + 1
                    finally:
                        report["derivation_compile_seconds"] += (
                            time.perf_counter() - start
                        )
                    if any(
                        b["verdict"]["refused"] or b["bound"] or not b["packages"]
                        for b in built
                    ):
                        raise ValueError(
                            "sample verifier refusal or non-relocatable package"
                        )
                meshes = set(built[0]["packages"])
                if any(set(b["packages"]) != meshes for b in built):
                    raise ValueError("structure: mesh set")
                templates = {}
                for mesh in sorted(meshes):
                    templates[mesh] = rs._rs.template_derive(
                        [b["packages"][mesh]["bytes"] for b in built],
                        json.dumps(pts),
                        json.dumps(bounds),
                        machine,
                        mesh,
                        values_json=recipes_json,
                    )
                for (launch, _), p in zip(group, params):
                    p = json.loads(rs._rs.template_values(recipes_json, json.dumps(p)))
                    if set(direct[launch]["packages"]) != meshes:
                        raise ValueError(f"structure: launch {launch} mesh set")
                    for mesh, template in templates.items():
                        package = direct[launch]["packages"][mesh]
                        bindings = [
                            0x10000000 + i * 0x100000
                            for i in range(len(package["params"]))
                        ]
                        rs._rs.template_prove(
                            template["bytes"],
                            template["template_json"],
                            package["bytes"],
                            json.dumps(p),
                            bindings,
                            machine,
                            f"launch {launch}",
                            mesh,
                        )
                index = len(result["artifacts"])
                entry = {
                    "generator": family,
                    "launches": [x[0] for x in group],
                    "bounds": bounds,
                    "values": recipes,
                    "meshes": {},
                }
                for mesh, template in templates.items():
                    stem = f"{index:04d}-{family}-{mesh}"
                    (artifacts / f"{stem}.bin").write_bytes(template["bytes"])
                    (artifacts / f"{stem}.json").write_text(
                        template["template_json"], encoding="utf-8"
                    )
                    entry["meshes"][mesh] = stem
                result["artifacts"].append(entry)
                report["experimental_templates"] += 1
                report["proven"] += len(group)
            except (ValueError, RuntimeError, KeyError) as e:
                error = str(e)
                result["failures"].append(
                    {
                        "generator": family,
                        "launches": [x[0] for x in group],
                        "phase": "derive",
                        "bounds": bounds,
                        "error": error,
                    }
                )
                if "non-affine" in error:
                    report["non_affine_fields"].append(error)
                varying = [n for n, b in bounds.items() if b["lo"] != b["hi"]]
                if not varying:
                    return
                dim = max(varying, key=lambda n: len({p[n] for p in params}))
                values = sorted({p[dim] for p in params})
                pivot = values[len(values) // 2]
                category = (
                    "choice_splits"
                    if "L2.TEMPLATE.CHOICE" in error
                    else (
                        "structure_splits" if "structure:" in error else "other_splits"
                    )
                )
                report[category] += 1
                derive([x for x, p in zip(group, params) if p[dim] < pivot])
                derive([x for x, p in zip(group, params) if p[dim] >= pivot])

        for group in family_groups:
            derive(group)
        result["wall_seconds"] = time.perf_counter() - started
        output.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"{family}: {json.dumps(report)}", flush=True)
    print(f"saved experimental results to {output}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("capture", type=pathlib.Path)
    ap.add_argument("output", type=pathlib.Path)
    ap.add_argument(
        "--shape-class", choices=("pow2", "exact", "coarse"), default="pow2"
    )
    ap.add_argument("--max-block", type=int, default=vector.MAX_BLOCK)
    ap.add_argument("--unpinned", action="store_true", help="direct-compile control")
    ap.add_argument("--class-rows", choices=("all", "min"))
    ap.add_argument("--class-flat", choices=("all", "min"))
    ap.add_argument("--class-mm", choices=("all", "min"))
    ap.add_argument("--class-quant", choices=("all", "min"))
    ap.add_argument("--class-walk", choices=("all", "min"))
    ap.add_argument("--class-copy", choices=("all", "min"))
    ap.add_argument("--class-transpose", choices=("all", "min"))
    args = ap.parse_args()
    run(
        args.capture,
        args.output,
        args.shape_class,
        args.max_block,
        args.unpinned,
        args.class_rows,
        args.class_flat,
        args.class_mm,
        args.class_quant,
        args.class_walk,
        args.class_copy,
        args.class_transpose,
    )


if __name__ == "__main__":
    main()
