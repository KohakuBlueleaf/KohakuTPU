"""Print the first structural disagreement in a captured rows class."""

import argparse
import json
import pathlib

from derive import decode, with_parameters

from kohakutpu.application.kernels import vector
from kohakutpu.application.kernels.classes.policy import ShapeClass
from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=pathlib.Path)
    parser.add_argument("results", type=pathlib.Path)
    args = parser.parse_args()
    capture = json.loads(args.capture.read_text(encoding="utf-8"))
    results = json.loads(args.results.read_text(encoding="utf-8"))
    fail = next(
        x
        for x in results["failures"]
        if x["phase"] == "derive" and "structure:" in x["error"]
    )
    record = capture["records"][capture["launches"][fail["launches"][0]]["record"]]
    print("failure", fail)
    target = OfflineCard(capture["board"]).machine
    points = json.loads(rs._rs.template_points(json.dumps(fail["bounds"])))
    samples = []
    for i, point in enumerate(points):
        kernel, given, _ = vector.rows_kernel(
            **decode(with_parameters(record["arguments"], point)),
            shape_class=ShapeClass(32, vector.MAX_BLOCK),
        )
        graph = kernel.trace(given, machine=K.dsl_machine(target)).graph
        result = rs._rs.compile(
            json.dumps(dict(graph, name="template")),
            capture["machine_json"],
            cache_dir="",
            fetch=True,
        )
        package = rs._rs.package_read(
            next(iter(result["packages"].values()))["bytes"], capture["machine_json"]
        )
        samples.append(package)
        if i and samples[0]["steps"] != package["steps"]:
            print("first divergent point", i, point)
            print("baseline steps", samples[0]["steps"])
            print("new steps", package["steps"])
            print(
                "baseline payloads",
                len(samples[0]["payloads"]),
                "new",
                len(package["payloads"]),
            )
            print(result["l1"])
            break


if __name__ == "__main__":
    main()
