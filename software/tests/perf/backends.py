"""Backend selection and completion observation for the existing model recorder."""

import json
import pathlib
import sys
from typing import Any

TOOLS = pathlib.Path(__file__).resolve().parents[2] / "kohakutpu/os/tools"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS.parents[2] / "kohakuaccel/os/tools"))

from ostpu.model import ModelResidents

from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.card.measure import Ring
from kohakutpu.application.runtime.os.graph import task_capacity


def completion_record(got, backend):
    """One completion, with every node retained rather than counted as a run."""
    record = dict(got.get("phases") or {})
    if "nodes" in got:
        nodes = {}
        for node, result in got["nodes"].items():
            values = dict(result.get("phases") or {})
            values.update(
                {k: v for k, v in result.items() if k not in ("phases", "pkg", "steps")}
            )
            nodes[node] = values
        record["nodes"] = nodes
    record.update(
        backend=backend,
        cycles=got["cycles"],
        max_node_cycles=got["cycles"],
        cycle_scope=(
            "kds_submit_to_reply" if backend == "kaos" else "firmware_package"
        ),
        cycle_aggregation="maximum_node_local_duration_not_global_makespan",
    )
    return record


def observe(out, backend, measure=False):
    """Observe public device retirement and reads, shared by both execution paths."""
    records, devices, rings = [], [], []
    init, collect, read = D.Device.__init__, D.Device._collect_one, D.Device.read
    output = (out / "outputs.jsonl").open("w", encoding="utf-8")

    def on_init(self, *args, **kwargs):
        init(self, *args, **kwargs)
        devices.append(self)
        if measure and backend == "firmware":
            rings.append(Ring(self.card))

    def on_collect(self, submitted):
        fresh = submitted.run is None
        run = collect(self, submitted)
        if fresh:
            records.append((run, completion_record(submitted.result, backend)))
        return run

    def on_read(self, address, count):
        raw = read(self, address, count)
        output.write(json.dumps(dict(backend=backend, bytes=raw.hex())) + "\n")
        output.flush()
        return raw

    # Recording wraps: the wrappers take the methods' arguments untyped.
    device: Any = D.Device
    device.__init__ = on_init
    device._collect_one = on_collect
    device.read = on_read

    def write():
        output.close()
        rows = [
            dict(name=r.name, kind=r.kind, wall=r.wall, seq=r.info.get("seq"), **values)
            for r, values in records
        ]
        (out / "card.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
        if measure and backend == "kaos":
            (out / "units.jsonl").write_text(
                "".join(
                    json.dumps(dict(backend=backend, run=i, node=node, **values)) + "\n"
                    for i, (_, record) in enumerate(records)
                    for node, values in record["nodes"].items()
                ),
                encoding="utf-8",
            )
        if rings:
            (out / "units.jsonl").write_text(
                "".join(
                    json.dumps(dict(backend=backend, **row)) + "\n"
                    for row in rings[-1].rows()
                ),
                encoding="utf-8",
            )
        if not devices:
            return {}
        device = devices[-1]
        return dict(counters=dict(device.counters), runs=len(device.runs))

    return write


def select(args, out):
    """Device construction stays in the workload; only its card backend changes."""
    init = D.Device.__init__
    args.os_capacity = task_capacity(args.os_capacity)
    boots = []

    def on_init(self, kind=None, *positional, **kwargs):
        kwargs["graph_mode"] = args.backend
        if args.backend == "kaos":
            boot = ModelResidents(
                args.board,
                args.nodes,
                args.os_board,
                args.kos_image,
                args.os_capacity,
                args.os_bytes,
                out,
            )
            boots.append(boot)
            kind = "os"
            kwargs.update(
                os_session=boot.sessions,
                os_units=boot.units,
                os_memory=boot.card.mem,
                os_arena=boot.arena,
            )
        init(self, kind, *positional, **kwargs)
        if args.backend == "kaos":
            self.card.sys_hz = boots[-1].sys_hz

    D.Device.__init__ = on_init

    def close():
        for boot in boots:
            boot.close()

    return close
