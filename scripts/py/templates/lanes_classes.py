"""Derive lanes classes and byte-prove every captured class instance."""

import argparse
import json
import pathlib
import time

from kohakutpu.application.kernels import lanes as L
from kohakutpu.application.kernels.classes import lanes as C
from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.application.runtime.template_store import TemplateStore
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def parameters(case):
    result = {"blocks": max(1, -(-case["words"] // 64))}
    for i, (rot, _) in enumerate(case["taps"]):
        result[f"rotation{i}"] = rot
    return result


def rotation_recipes(taps):
    def constant(value):
        return {"op": "const", "value": value}

    def add(a, b):
        return {"op": "add", "a": a, "b": b}

    def scale(a, n):
        return {"op": "mul", "a": a, "b": constant(n)}

    def div(a):
        return {"op": "div", "a": a, "divisor": 16}

    recipes = {}
    for i in range(taps):
        r = {"op": "param", "name": f"rotation{i}"}
        eq_one = add(div(add(r, constant(15))), scale(div(add(r, constant(14))), -1))
        first = add(constant(1), eq_one)
        difference = add(r, scale(first, -1))
        recipes[f"first{i}"] = first
        recipes[f"second{i}"] = add(difference, scale(div(difference), -16))
    return recipes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=pathlib.Path)
    parser.add_argument("output", type=pathlib.Path)
    parser.add_argument("--max-taps", type=int, default=16)
    parser.add_argument("--min-taps", type=int, default=1)
    parser.add_argument("--probe-only", action="store_true")
    parser.add_argument("--board")
    args = parser.parse_args()
    doc = json.loads(args.capture.read_text(encoding="utf-8"))
    machine = OfflineCard(args.board or doc["board"]).machine
    machine_json = rs.machine_json(machine)
    groups, launches = {}, set()
    for launch_id, launch in enumerate(doc["launches"]):
        record = doc["records"][launch["record"]]
        if record["generator"] != "lanes":
            continue
        launches.add(launch_id)
        for cls in record["arguments"]["pl"]["fields"]["classes"]:
            c = cls["fields"]
            groups.setdefault((len(c["taps"]), c["keep"]), []).append(c)
    results = []
    for (taps, keep), cases in sorted(groups.items()):
        if not args.min_taps <= taps <= args.max_taps:
            continue
        started = time.perf_counter()
        maximum = max(c["words"] for c in cases)
        box = {"blocks": {"lo": 1, "hi": max(2, -(-maximum // 64))}}
        for i in range(taps):
            box[f"rotation{i}"] = {
                "lo": 0,
                "hi": json.loads(machine_json)["machine"]["vc"]["lanes"] - 1,
            }
        recipes = rotation_recipes(taps)

        def graph(point, taps=taps, keep=keep, recipes=recipes):
            point = json.loads(
                rs._rs.template_values(json.dumps(recipes), json.dumps(point))
            )
            cls = L.Class(
                tuple((2, 1 << (i % 16)) for i in range(taps)),
                point["blocks"] * 64,
                keep,
            )
            rotations = [(point[f"first{i}"], point[f"second{i}"]) for i in range(taps)]
            kernel, given = C.kernel(
                cls, 65536, 65536, machine.count("VC"), rotations=rotations
            )
            traced = kernel.trace(given, machine=K.dsl_machine(machine)).graph
            return json.dumps(dict(traced, name="template"))

        row = {
            "taps": taps,
            "keep": keep,
            "instances": len(cases),
            "max_words": maximum,
            "proven": 0,
        }
        try:
            hazard_box = {
                name: (
                    bounds
                    if name == "blocks"
                    else {"lo": bounds["lo"], "hi": bounds["lo"]}
                )
                for name, bounds in box.items()
            }
            points = json.loads(rs._rs.template_points(json.dumps(hazard_box)))
            origin = {name: bounds["lo"] for name, bounds in box.items()}
            for i in range(taps):
                for rotation in range(box[f"rotation{i}"]["hi"] + 1):
                    point = dict(origin, **{f"rotation{i}": rotation})
                    if point not in points:
                        points.append(point)
            if args.probe_only:
                points = points[:5]
            graphs = [graph(p) for p in points]
            compiled = rs._rs.template_compile(
                graphs[0], graphs, machine_json, parameter_rotations=True
            )
            if args.probe_only or taps == 1:
                evidence = args.output.parent / f"lanes-structure-{taps}"
                evidence.mkdir(parents=True, exist_ok=True)
                for i in range(min(5, len(compiled))):
                    (evidence / f"point-{i}.json").write_text(
                        json.dumps(points[i]), encoding="utf-8"
                    )
                    (evidence / f"point-{i}.l1").write_text(
                        compiled[i]["l1"], encoding="utf-8"
                    )
                    decoded = rs._rs.package_read(
                        compiled[i]["packages"][0]["bytes"], machine_json
                    )
                    (evidence / f"steps-{i}.json").write_text(
                        json.dumps(decoded["steps"], indent=2), encoding="utf-8"
                    )
            template = rs._rs.template_derive(
                [c["packages"][0]["bytes"] for c in compiled],
                json.dumps(points),
                json.dumps(box),
                machine_json,
                values_json=json.dumps(recipes),
                confined=True,
            )
            row["sites"] = len(json.loads(template["template_json"])["sites"])
            package = rs._rs.package_read(template["bytes"], machine_json)
            bindings = [
                0x10000000 + i * 0x1000000 for i in range(len(package["buffers"]))
            ]
            store = TemplateStore("build/templates", machine_json)
            row["library_class"] = store.admit(
                graphs[0], template["bytes"], template["template_json"], bindings
            )
            template["bytes"], template["template_json"] = store.load(
                graphs[0], template["template_json"]
            )
            unique = {json.dumps(parameters(c), sort_keys=True) for c in cases}
            for raw in sorted(unique):
                p = json.loads(raw)
                direct = rs._rs.template_compile(
                    graphs[0], [graph(p)], machine_json, parameter_rotations=True
                )[0]
                assert not direct["verdict"]["refused"], direct["verdict"]
                package = rs._rs.package_read(template["bytes"], machine_json)
                bindings = [
                    0x10000000 + i * 0x1000000 for i in range(len(package["buffers"]))
                ]
                rs._rs.template_prove(
                    template["bytes"],
                    template["template_json"],
                    direct["packages"][0]["bytes"],
                    rs._rs.template_values(json.dumps(recipes), raw),
                    bindings,
                    machine_json,
                    raw,
                )
                row["proven"] += sum(parameters(c) == p for c in cases)
            row["confinement"] = json.loads(template["confinement_json"])
            verified = rs._rs.template_verify(
                {0: template["bytes"]},
                {0: template["template_json"]},
                {0: bindings},
                machine_json,
                f"lanes-{taps}-{keep}",
                confined=True,
            )
            row["verified_bindings"] = len(json.loads(verified))
            row["verification"] = (
                "confinement-reduced corners/adjacent/seeded checker, twice per binding"
            )
            artifact = args.output.parent / "lanes-artifacts" / f"{taps}-{keep}"
            artifact.mkdir(parents=True, exist_ok=True)
            (artifact / "params.json").write_text(
                json.dumps(compiled[0]["packages"][0]["params"]), encoding="utf-8"
            )
            (artifact / "source.json").write_text(graphs[0], encoding="utf-8")
            (artifact / "package.bin").write_bytes(template["bytes"])
            (artifact / "table.json").write_text(
                template["template_json"], encoding="utf-8"
            )
            (artifact / "confinement.json").write_text(
                template["confinement_json"], encoding="utf-8"
            )
        except (ValueError, AssertionError) as error:
            row["error"] = str(error)
        row["seconds"] = time.perf_counter() - started
        results.append(row)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(
                {
                    "launches": len(launches),
                    "class_shapes": len(groups),
                    "results": results,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(
            {key: value for key, value in row.items() if key != "confinement"},
            flush=True,
        )
    if any("error" in result for result in results):
        raise RuntimeError("lanes class derivation refused; see report")


if __name__ == "__main__":
    main()
