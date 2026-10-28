"""Export quant schedule-only recordings; independent name/graph delta checks."""

import copy
import json
import shutil
from pathlib import Path

from kohakutpu.application.kernels.vector import Operand, name
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import rs

ROOT = Path(__file__).resolve().parents[3]
SCHEDULE = (("finite_batch",4),("overlap",1),("classifier_overlap",0),("pipeline",1),("zero_classifier",1),("stream_axes",1),("fill_overlap",1),("zero_reuse",1))


def strip(value):
    if isinstance(value, dict):
        return {k: strip(v) for k, v in value.items() if k != "where"}
    if isinstance(value, list):
        return [strip(v) for v in value]
    return value


def add_schedule(body):
    for node in body:
        if node.get("op") == "quant":
            assert all(k not in dict(SCHEDULE) for k, _ in node["kw"])
            node["kw"].extend([[k, {"int": v}] for k,v in SCHEDULE])
        add_schedule(node.get("body", []))


def schedule_only(old, new, expected_name):
    expected = copy.deepcopy(strip(old))
    expected["name"] = expected_name
    add_schedule(expected["body"])
    assert expected == strip(new), dict(expected=expected, actual=strip(new))


def main():
    output = ROOT / ".plan/vq/loop8/kernel-regold"
    files = []
    for path in sorted((ROOT / "software/rust/golden/kernels").glob("*.json")):
        doc = json.loads(path.read_text())
        changed = 0
        for call in doc["calls"]:
            if call["fn"] != "matmul.quant_kernel" or "graph" not in call:
                continue
            args = dict(call["args"], src=call["args"]["src"]["operand"])
            expected_name = name("quant", Operand(**args["src"]), *(args[k] for k in ("rows","cols","rp","cp","side","g","nk","batches")), False, SCHEDULE)
            rec = call["machine"]
            machine = machine_of(rec["board"], nodes=rec["nodes"])
            graph = json.loads(rs._rs.generate(call["fn"],json.dumps(args),rs.machine_json(machine),None))
            if strip(graph) == strip(call["graph"]):
                continue
            schedule_only(call["graph"],graph,expected_name)
            assert call["given"] == {p["name"]: p["spec"] for p in graph["ports"]}
            call["name"] = graph["name"]
            call["graph"] = graph
            if isinstance(call.get("nodes2"),dict):
                graph2 = json.loads(rs._rs.generate(call["fn"],json.dumps(args),rs.machine_json(machine_of(rec["board"],nodes=2)),None))
                schedule_only(call["nodes2"],graph2,expected_name)
                call["nodes2"] = graph2
            changed += 1
        if not changed:
            continue
        for directory in ("originals", "kernels"):
            (output / directory).mkdir(parents=True,exist_ok=True)
        with (output / "originals" / path.name).open("xb") as f:
            f.write(path.read_bytes())
        dest = output / "kernels" / path.name
        dest.write_text(json.dumps(doc,indent=2)+"\n",encoding="utf-8")
        shutil.copyfile(dest,path)
        files.append(dict(file="kernels/"+path.name,calls=changed))
    (output / "manifest.json").write_text(json.dumps(files,indent=2),encoding="utf-8")
    print(json.dumps(dict(files=len(files),calls=sum(f["calls"] for f in files))))


if __name__ == "__main__":
    main()
