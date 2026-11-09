"""Offline KAOS preflight: every ViT-B launch package through the KAOS translator."""

import collections
import json
import pathlib
import sys

sys.path[:0] = [str(pathlib.Path.home() / "kt/software/tests/perf")]
import record  # noqa: E402

from kohakutpu.application.runtime import offline as O  # noqa: E402
from kohakutpu.application.runtime.os import abi as A  # noqa: E402
from kohakutpu.application.runtime.os.graph import Graph, task_capacity  # noqa: E402
from kohakutpu.application.runtime.split import BoundGraphArtifact, NodePart  # noqa: E402

board, nodes, batch = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
cap = task_capacity(65534)
out = pathlib.Path(f"/tmp/sp/kpre.{board}.p{nodes}.b{batch}.jsonl")
out.unlink(missing_ok=True)
stats = collections.Counter()
built_fn = O.B.built


def checked(progs, machine, *a, **kw):
    both = built_fn(progs, machine, *a, **kw)
    built = {n: (pkg, b) for n, (_, pkg, b) in both.items()}
    self = checked
    self.machine = machine
    units = getattr(self, "_kpre_units", None)
    if units is None:
        units, types, i = {}, {}, 0
        for mesh in self.machine.meshes:
            if mesh.index >= nodes:
                continue
            for kind, coords in mesh.units.items():
                for coord in coords:
                    units[(mesh.index, *coord)] = i
                    types[(mesh.index, *coord)] = int.from_bytes(kind.encode(), "big")
                    i += 1
        self._kpre_units, self._kpre_types = units, types
    artifact = BoundGraphArtifact(
        "preflight", tuple(NodePart(n, p.to_bytes()) for n, (p, _) in built.items())
    )
    row = dict(nodes=sorted(built))
    try:
        graphs = Graph.package_parts(
            artifact, units, node_ids={n: n for n in built}, unit_types=self._kpre_types
        )
        for n, g in graphs.items():
            kinds = collections.Counter(t.kind for t in g.tasks)
            raw = g.pack(0)
            row[str(n)] = dict(tasks=len(g.tasks), bytes=len(raw), kinds=dict(kinds))
            if not 0 < len(g.tasks) <= cap or len(raw) > cap * A.OSTPU_TASK_BYTES:
                row["refused"] = f"node {n}: {len(g.tasks)} tasks {len(raw)} bytes over capacity {cap}"
        stats["ok" if "refused" not in row else "capacity"] += 1
    except Exception as error:  # noqa: BLE001
        row["refused"] = f"{type(error).__name__}: {error}"
        stats[row["refused"][:120]] += 1
    with out.open("a") as f:
        f.write(json.dumps(row) + "\n")
    return both


O.B.built = checked
sys.argv = ["record.py", "vitb", "--board", board, "--nodes", str(nodes), "--batch", str(batch),
            "--steps", "1", "--blocks", "12", "--tag", f".kpre.b{batch}"]
try:
    record.main()
finally:
    print("PREFLIGHT", board, nodes, batch, dict(stats), flush=True)
