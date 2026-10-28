"""Inventory exact template parameter influence from ISA confinement certificates."""

import argparse
import json
import math
import time
from pathlib import Path

from kohakutpu.application.runtime.device import Device
from kohakutpu.stack import rs


def inventory(library, capture, caps=()):
    device = Device(kind="offline", board=capture["board"])
    machine = rs.machine_json(device.machine)
    device.close()
    source = next(iter(capture["sources"].values()))["source"]
    key = json.loads(rs._rs.library_key(source, machine))
    rows = {}
    for path in sorted((library / key["machine"] / "classes").glob("*.json")):
        if path.name == "index.json":
            continue
        entry = json.loads(path.read_text(encoding="utf-8"))
        table = json.loads(entry["table"])
        certificate = json.loads(
            rs._rs.template_certificate(
                bytes.fromhex(entry["package"]), entry["table"], machine
            )
        )
        package = rs._rs.package_read(bytes.fromhex(entry["package"]), machine)
        dimensions = {}
        for name, bounds in table["parameters"].items():
            sites = [
                dict(site=site, field=field, field_class=kind)
                for site, field, kind in zip(
                    table["sites"], certificate["sites"], certificate["classes"]
                )
                if site["coefficients"].get(name, 0)
            ]
            for influence in sites:
                site = influence["site"]
                if influence["field"].startswith("MOVER.DIM."):
                    half = site["bit"] // 128 * 128
                    value = (package["payloads"][site["payload"]] >> (half + 64)) & (
                        (1 << 64) - 1
                    )
                    influence["walker"] = "dst" if value & 1 else "src"
                    influence["level"] = (value >> 1) & 7
                    influence["checker_properties"] = (
                        [
                            "L1.VER.017 destination alignment/range, visit budget and competing partial owners"
                        ]
                        if value & 1
                        else [
                            "L1.VER.017 transform source-count ratio; ordinary source stride has no modeled events"
                        ]
                    )
            dimensions[name] = dict(
                **bounds,
                width=bounds["hi"] - bounds["lo"] + 1,
                data_only=certificate["data_only"][name],
                sites=sites,
            )
        measurements = []
        if (
            math.prod(
                dim["width"] for dim in dimensions.values() if not dim["data_only"]
            )
            > 256
        ):
            for cap in caps:
                started = time.perf_counter()
                verdict = json.loads(
                    rs._rs.template_verify(
                        {0: bytes.fromhex(entry["package"])},
                        {0: entry["table"]},
                        {0: entry["bindings"]},
                        machine,
                        path.stem,
                        mode="proof",
                        cap=cap,
                    )
                )
                measurements.append(
                    dict(
                        cap=cap, seconds=time.perf_counter() - started, verdict=verdict
                    )
                )
        rows[path.stem] = dict(
            measurements=measurements,
            steps=package["steps"],
            payloads=package["payloads"],
            dimensions=dimensions,
            exhaustive_product=math.prod(
                dim["width"] for dim in dimensions.values() if not dim["data_only"]
            ),
        )
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--measure-cap", type=int, action="append", default=[])
    args = parser.parse_args()
    capture = json.loads(args.capture.read_text(encoding="utf-8"))
    rows = inventory(args.library, capture, args.measure_cap)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    for name, row in rows.items():
        if row["exhaustive_product"] <= 256:
            continue
        print(name, "product", row["exhaustive_product"], "steps", row["steps"])
        for measurement in row["measurements"]:
            verdict = measurement["verdict"]
            cert = verdict.get("certificate", {})
            print(
                "cap",
                measurement["cap"],
                verdict["status"],
                measurement["seconds"],
                "bindings",
                cert.get("checker_bindings"),
                "simulations",
                cert.get("simulation_bindings"),
            )
        for parameter, dim in row["dimensions"].items():
            fields = sorted({(s["field_class"], s["field"]) for s in dim["sites"]})
            print(
                parameter,
                (dim["lo"], dim["hi"]),
                "eliminated" if dim["data_only"] else "relevant",
                fields,
            )


if __name__ == "__main__":
    main()
