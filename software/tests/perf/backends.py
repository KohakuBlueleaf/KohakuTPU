"""Backend selection and completion observation for the existing model recorder."""

import pathlib
import sys

TOOLS = pathlib.Path(__file__).resolve().parents[2] / "kohakutpu/os/tools"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS.parents[2] / "kohakuaccel/os/tools"))

from ostpu.model import ModelResidents
from retirement import observe as retirements

from kohakutpu.application.runtime import device as D
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
    return retirements(out, backend, measure, outputs=True)


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
            self.card._perf_windows = dict(enumerate(boots[-1].windows))
            self.card._perf_transport = boots[-1].card.t

    D.Device.__init__ = on_init

    def close():
        for boot in boots:
            boot.close()

    return close
