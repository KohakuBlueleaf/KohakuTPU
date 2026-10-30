"""The plan goldens (`software/rust/golden/plan`) as the regolds read, compare
and write them: `planner_record`'s recordings, the tests' comparison
(`ktpu-plan/tests/common`'s `normal`), each golden's own text layout."""

import functools
import hashlib
import json
import subprocess
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
RUST = ROOT / "software/rust"
PLAN = RUST / "golden/plan"
KERNELS = RUST / "golden/kernels"


def record(output, select, *extra):
    """`planner_record` of the goldens holding `select` into `output`."""
    subprocess.run(
        ["cargo", "run", "-p", "ktpu-plan", "--example", "planner_record", "--"]
        + [str(output), "--select", select, *extra],
        cwd=RUST,
        check=True,
    )


def table(arr) -> dict:
    """`forms.table`'s upload of `arr` as the planner records it."""
    a = np.ascontiguousarray(arr)
    key = hashlib.sha1(a.tobytes() + repr((a.shape, a.dtype)).encode()).hexdigest()
    at = {"buf": f"table:{key}", "form": ["flat"], "offset": 0}
    array = {
        "dtype": str(a.dtype),
        "shape": list(a.shape),
        "sha256": hashlib.sha256(a.tobytes()).hexdigest(),
    }
    return {"table": {"at": at, "array": array}}


def bare(node):
    """`node` without its graph `where` fields."""
    if isinstance(node, dict):
        return {k: bare(v) for k, v in node.items() if k != "where"}
    if isinstance(node, list):
        return [bare(v) for v in node]
    return node


def entries(rec):
    """`rec`'s step lists: its runs, direct rows calls and kp entries."""
    return rec["runs"] + rec.get("rows", []) + rec["kp"]


def renamed(node, names):
    """`node` with each temporary (`tmpN`, `rawN`) named by first use, keys
    in sorted order: a Python run's `tmpN` count starts where the process's
    stood, a Rust run's at 1."""
    if isinstance(node, str) and node[:3] in ("tmp", "raw"):
        return names.setdefault(node, f"{node[:3]}#{len(names)}")
    if isinstance(node, dict):
        return {k: renamed(node[k], names) for k in sorted(node)}
    if isinstance(node, list):
        return [renamed(v, names) for v in node]
    return node


def normal(rec):
    """Recording `rec` as `ktpu-plan`'s tests compare it: no `where`, each
    step list's temporaries named by first use outside the launch graphs."""
    rec = bare(rec)
    for entry in entries(rec):
        names = {}
        steps = []
        for s in entry["steps"]:
            graph = s.get("launch", {}).pop("graph", None)
            s = renamed(s, names)
            if graph is not None:
                s["launch"]["graph"] = graph
            steps.append(s)
        entry["steps"] = steps
    return rec


@functools.cache
def ruled_graphs() -> dict:
    """Kernel name -> its graph as RUL.060 rules a truncation
    (`golden/kernels`, each call's `ruled`)."""
    out = {}
    for path in sorted(KERNELS.glob("*.json")):
        for c in json.loads(path.read_text("utf-8")).get("calls", []):
            r = c.get("ruled") or {}
            if "graph" in r:
                out[r["name"]] = r["graph"]
    return out


def ruled(rec):
    """Golden `rec` as `ktpu-plan`'s tests take it (`common`'s `ruled`):
    each launch whose expression truncates carries its RUL.060 graph, the
    launch's machine kept."""
    rec = json.loads(json.dumps(rec))
    for entry in entries(rec):
        for s in entry["steps"]:
            launch = s.get("launch") or {}
            call = json.dumps(launch.get("call"), separators=(",", ":"))
            if '"op":"trunc"' not in call or "graph" not in launch:
                continue
            graph = ruled_graphs()[launch["graph"]["name"]]
            launch["graph"] = dict(graph, machine=launch["graph"]["machine"])
    return rec


def want(golden):
    """Golden `golden` as `ktpu-plan`'s tests compare a recording to it."""
    return normal(ruled(golden))


def keep_old(old, new):
    """`new`, with `old`'s subtree wherever the two differ only in `where`."""
    if bare(old) == bare(new):
        return old
    if isinstance(old, dict) and isinstance(new, dict):
        return {k: keep_old(old[k], v) if k in old else v for k, v in new.items()}
    if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        return [keep_old(a, b) for a, b in zip(old, new)]
    return new


def layout(doc, raw) -> dict:
    """The `json.dumps` keywords `raw` (`doc`'s text) is written with: its
    indent, keys sorted or as they are."""
    line = raw.split("\n")[1]
    indent = len(line) - len(line.lstrip())
    return next(
        s
        for s in ({"indent": indent, "sort_keys": True}, {"indent": indent})
        if json.dumps(doc, **s) + "\n" == raw
    )
