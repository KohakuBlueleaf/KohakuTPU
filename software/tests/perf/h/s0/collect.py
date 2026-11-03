"""Every launch of a point labelled and its card records kept: a label a launch in
submission order (a replay's from its log), the card's record a collected run
(`record.card_rows`), and the measure ring's record a launch with each unit's
busy/retired change since that unit's previous record."""

import record

from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.card import NodeCard
from kohakutpu.application.runtime.card.measure import Ring

#: CU_COUNTERS halves are 32-bit cumulative counts.
WRAP = 1 << 32


def unit_name(type_: int) -> str:
    """A unit's type word as its two letters ("MG", "VC")."""
    return chr(type_ >> 8 & 0xFF) + chr(type_ & 0xFF)


def differenced(last: dict, row: dict) -> dict:
    """`row`'s units, each with its busy/retired change since that unit's record
    in `last` (none: zero), which then holds this one."""
    for u in row["units"]:
        key = (row["node"], u["x"], u["y"])
        busy, retired = last.get(key, (u["busy"], u["retired"]))
        u["unit"] = unit_name(u["type"])
        u["d_busy"] = (u["busy"] - busy) % WRAP
        u["d_retired"] = (u["retired"] - retired) % WRAP
        last[key] = (u["busy"], u["retired"])
    return row


def install(col: "Collector") -> None:
    """Device submits and replays label their launches into `col`, the card's
    results append its records."""
    submit, replay, result = D.Device.submit, D.Device.replay, NodeCard._result

    # A label is taken when its submit returns: a launch it flushed went first.
    def on_submit(self, compiled, binds, kind="dsl", info=None):
        got = submit(self, compiled, binds, kind, info)
        got.info["s0"] = dict(col.tag)
        col.labels.append(dict(col.tag, name=compiled.name, kind=kind, log=-1))
        return got

    def on_replay(self, log, *args, **kw):
        got = replay(self, log, *args, **kw)
        for event in got:
            event.info["s0"] = dict(col.tag)
        col.replays += 1
        col.labels += [
            dict(col.tag, name=r[0], kind=r[1], log=i) for i, r in enumerate(log.runs)
        ]
        return got

    def on_result(self, *args, **kw):
        got = result(self, *args, **kw)
        col.records.append(dict(got.get("phases") or {}, cycles=got["cycles"]))
        return got

    D.Device.submit = on_submit
    D.Device.replay = on_replay
    NodeCard._result = on_result


class Collector:
    """`tag` (case, R, variant, call) marks every launch made while it is set;
    `flush` returns the point's card rows and ring rows, labelled."""

    def __init__(self, sysnode: int) -> None:
        self.sysnode = sysnode
        self.tag: dict = {}
        self.labels: list[dict] = []
        self.records: list[dict] = []
        self.replays = 0
        self.runs_at = self.records_at = 0
        self.last: dict = {}
        self.ring: Ring | None = None
        install(self)

    def flush(self, dev) -> tuple[list[dict], list[dict], dict]:
        """The point's card rows and ring rows, labelled, and how the ring's
        records of the sysnode met the labels."""
        dev.drain()
        runs, self.runs_at = dev.runs[self.runs_at :], len(dev.runs)
        records, self.records_at = self.records[self.records_at :], len(self.records)
        labels, self.labels = self.labels, []
        ring = (
            [differenced(self.last, r) for r in self.ring.rows()] if self.ring else []
        )
        mine = [r for r in ring if r["node"] == self.sysnode]
        joined = len(mine) == len(labels)
        for r, label in zip(mine, labels if joined else [], strict=False):
            r.update(label)
        if records:
            cycles = [r["cycles"] for r in mine] if joined and self.ring else None
            rows = record.card_rows(runs, records, cycles)
        else:
            rows = [
                dict(
                    name=r.name,
                    kind=r.kind,
                    wall=r.wall,
                    seq=r.info.get("seq"),
                    cycles=r.cycles,
                )
                for r in runs
            ]
        tags = {r.info.get("seq"): r.info.get("s0", {}) for r in runs}
        rows = [dict(tags.get(row["seq"], {}), **row) for row in rows]
        return rows, ring, dict(labels=len(labels), ring=len(mine), joined=joined)
