"""Export the graph-library artifact the runtime binds, not reassembled L1."""

import json

from kohakutpu.stack import rs


def stage(compiled, binds, library):
    """Prebuild `compiled` into the graph library at directory `library`."""
    source = (
        compiled.traced.graph_json if compiled.traced is not None else compiled.l2_text
    )
    graph = json.loads(source)
    result = rs.library_prebuild(
        str(library), source, rs.machine_json(compiled.machine)
    )
    key = json.loads(result["key"])
    entry = json.loads(
        (library / key["machine"] / (key["class"] + ".json")).read_text()
    )
    c = entry["compiled"]
    return {
        "name": c["name"],
        "l1": c["l1"],
        "verdict": c["verdict"],
        "binds": binds,
        "compiled": c,
        "inputs": [p["name"] for p in graph["ports"] if p["role"] == "in"],
        "library_key": key,
    }
