"""Identity-based retirement and per-sysnode measurement-ring joins.

No positional join between NodeCard._result calls and logical launches: a
Submitted owns a grouped result, even when several sysnodes completed it.
"""

import collections
import json
import os
from typing import Any

from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.card.measure import Ring
from kohakutpu.application.runtime.eager_batch import Deferred
from kohakutpu.driver.clock.card import CardClocks


def node_results(submitted, card):
    got = submitted.result
    if "nodes" in got:
        results = got["nodes"]
    else:
        nodes = getattr(submitted, "nodes", ()) or card.nodes[:1]
        if len(nodes) != 1:
            raise ValueError("flat completion for a multi-sysnode submission")
        results = {nodes[0]: got}
    return {
        str(node): dict(
            result.get("phases") or {},
            **{
                k: v
                for k, v in result.items()
                if k not in ("phases", "pkg", "steps", "dump", "debug")
            },
        )
        for node, result in results.items()
    }


def card_rows(entries, ring=None):
    """One row per logical retirement; ring cursors are independent per sysnode.

    Entries retain Run objects so seq assigned after synchronous retirement is
    visible here. A Deferred eager-list member consumes one local ring entry;
    the list completion's overhead belongs only to its first member. A retained
    graph/list is one observable launch, never decomposed using capture timings.
    """
    streams = collections.defaultdict(collections.deque)
    for slot in ring or ():
        streams[str(slot["node"])].append(slot)
    out = []
    for entry in entries:
        run, nodes = entry["run"], entry["nodes"]
        deferred = entry.get("deferred", False)
        row = {
            "name": run.info.get("trace_name", run.name),
            "original_name": run.name,
            "kind": run.kind,
            "wall": run.wall,
            "seq": run.info.get("seq"),
            "input": run.info.get("input"),
            "backend": entry["backend"],
            "nodes": {},
        }
        for node, raw in nodes.items():
            values: dict[str, Any] = dict(raw)
            count = max(1, values.get("launches", values.get("list_index", 0)))
            take = 1 if deferred else count
            slots = []
            if ring is not None:
                if len(streams[node]) < take:
                    raise ValueError(
                        f"measure ring underflow: node {node}, seq {row['seq']}"
                    )
                slots = [streams[node].popleft() for _ in range(take)]
            if deferred and count > 1:
                if not slots:
                    raise ValueError("eager-list attribution requires its measure ring")
                total = values["cycles"]
                if not entry["first"]:
                    values = {
                        k: 0 for k in values if isinstance(values[k], (int, float))
                    }
                else:
                    values["list_cycles"] = total
                values.update(
                    cycles=slots[0]["cycles"],
                    steps=slots[0]["cycles"],
                    list_index=0,
                    in_list=count,
                )
            if slots:
                values["ring"] = slots
                for slot in slots:
                    slot.update(
                        row=len(out),
                        seq=row["seq"],
                        input=row["input"],
                        kind=row["kind"],
                    )
            row["nodes"][node] = values
        if not row["nodes"]:
            raise ValueError("retirement without a sysnode result")
        if any("cycles" not in v for v in row["nodes"].values()):
            raise ValueError("incomplete sysnode completion")
        row["cycles"] = row["max_node_cycles"] = max(
            v["cycles"] for v in row["nodes"].values()
        )
        row["cycle_aggregation"] = "maximum_node_local_duration_not_global_makespan"
        row["cycle_scope"] = (
            "kds_submit_to_reply" if entry["backend"] == "kaos" else "firmware_package"
        )
        row["list_index"] = 0
        # Firmware completion records also have a numeric `kind`. Never let
        # hardware fields overwrite the host launch kind, identity or cycles.
        timing = (
            "head",
            "copy",
            "bind",
            "steps",
            "barrier",
            "idle",
            "item_tag",
            "epoch",
            "list_index",
            "list_cycles",
            "in_list",
            "trace",
        )
        if len(row["nodes"]) == 1:
            only = next(iter(row["nodes"].values()))
            row.update({k: only[k] for k in timing if k in only})
        row["completion_phases"] = entry.get("phases", {})
        row.update(
            {
                k: v
                for k, v in entry.get("phases", {}).items()
                if k
                in (
                    "makespan_seconds",
                    "prepare_seconds",
                    "prepare_and_execute_seconds",
                )
            }
        )
        out.append(row)
    if ring is not None and any(streams.values()):
        raise ValueError(
            f"unmatched measure records: { {n: len(s) for n, s in streams.items() if s} }"
        )
    return out


def observe(out, backend, measure=False, outputs=False):
    """Observe successful retirements once. Flush rings before device teardown.

    Partial raw records are written before joining, so a failed join or a ring
    overflow remains inspectable and cannot silently shift later attributions.
    """
    devices, entries, slots, losses, clock_checks = [], [], [], [], []
    init, collect, close, read = (
        D.Device.__init__,
        D.Device._collect_one,
        D.Device.close,
        D.Device.read,
    )
    output = (out / "outputs.jsonl").open("w", encoding="utf-8") if outputs else None

    def check_clocks(dev, when):
        expected = json.loads(os.environ.get("PERF_CLOCKS", "null"))
        if expected is None:
            return
        card = dev.card
        transport = getattr(card, "_perf_transport", None) or card.t
        clocks = CardClocks(transport, card.board)
        actual = {
            str(n): dict(clock.read_all(), locked=clock.locked())
            for n, clock in enumerate(clocks.meshes)
        }
        clock_checks.append(
            {"when": when, "clocks": actual, "matches": actual == expected}
        )
        (out / "clock-checks.json").write_text(
            json.dumps(clock_checks, indent=1), "utf-8"
        )
        if actual != expected:
            raise ValueError("actual card clocks differ from PERF_CLOCKS readback")

    def raw_entry(entry):
        run = entry["run"]
        return dict(
            entry,
            run={
                "name": run.name,
                "kind": run.kind,
                "wall": run.wall,
                "info": {
                    k: v
                    for k, v in run.info.items()
                    if isinstance(v, (str, int, float, bool, type(None)))
                },
            },
        )

    def persist():
        # Boundary snapshots resolve late seq assignment; the append stream
        # below also survives a hard kill inside an input.
        for filename, values in (
            ("retirements.jsonl", map(raw_entry, entries)),
            ("units.jsonl", slots),
        ):
            (out / filename).write_text(
                "".join(json.dumps(r) + "\n" for r in values), "utf-8"
            )
        (out / "ring-loss.json").write_text(json.dumps(losses), "utf-8")

    def flush(dev):
        ring = getattr(dev, "_perf_ring", None)
        try:
            if ring is not None:
                slots.extend(ring.rows())
                if any(ring.lost.values()):
                    losses.append(dict(ring.lost))
        finally:
            persist()

    def on_init(self, *args, **kwargs):
        init(self, *args, **kwargs)
        devices.append(self)
        self._perf_flush = lambda: flush(self)
        check_clocks(self, "after_boot")
        if measure and backend != "kaos":
            self._perf_ring = Ring(self.card)

    def on_collect(self, s):
        submitted = s
        fresh = submitted.run is None
        run = collect(self, submitted)
        if fresh:
            pending = getattr(submitted, "pending", None)
            entries.append(
                {
                    "run": run,
                    "nodes": node_results(submitted, getattr(self, "card", None)),
                    "backend": backend,
                    "deferred": isinstance(pending, Deferred),
                    "first": getattr(pending, "first", True),
                    "phases": dict(submitted.result.get("phases") or {}),
                }
            )
            with (out / "retirements.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(raw_entry(entries[-1])) + "\n")
        return run

    def on_close(self, *args, **kwargs):
        # Healthy close would drain anyway. Do it before the final ring read;
        # never add a new drain/replay on an already-failed device.
        try:
            if not getattr(self, "closed", False):
                if not getattr(self, "_failure", None) and not getattr(
                    self, "_uncertain", ()
                ):
                    self.drain()
                    check_clocks(self, "before_close")
                flush(self)
        finally:
            persist()
            return_value = close(self, *args, **kwargs)
        return return_value

    def on_read(self, addr, nbytes):
        raw = read(self, addr, nbytes)
        if output is not None:
            output.write(json.dumps({"backend": backend, "bytes": raw.hex()}) + "\n")
            output.flush()
        return raw

    D.Device.__init__, D.Device._collect_one = on_init, on_collect
    D.Device.close, D.Device.read = on_close, on_read

    def write():
        if output is not None:
            output.close()
        persist()
        try:
            for dev in devices:
                if not getattr(dev, "closed", False):
                    if not getattr(dev, "_failure", None) and not getattr(
                        dev, "_uncertain", ()
                    ):
                        check_clocks(dev, "after_workload")
                    flush(dev)
        finally:
            persist()
        if losses:
            raise ValueError("measure ring overflow; raw retirements retained")
        rows = card_rows(entries, slots if measure and backend != "kaos" else None)
        (out / "card.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), "utf-8"
        )
        # Ring rows now also carry their launch/input identity. KAOS has
        # completion profile counters, not a firmware measure ring.
        units = (
            slots
            if backend != "kaos"
            else [
                dict(backend=backend, run=i, node=int(node), **values)
                for i, row in enumerate(rows)
                for node, values in row["nodes"].items()
            ]
        )
        (out / "units.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in units), "utf-8"
        )
        if not devices:
            return {}
        dev = devices[-1]
        return {
            "counters": dict(dev.counters),
            "runs": len(dev.runs),
            "retirement_rows": len(rows),
            "ring_lost": losses,
            "clock_checks": clock_checks,
        }

    return write
