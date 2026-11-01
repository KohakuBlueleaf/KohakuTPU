"""A packed run as Python: one row a launch, all layers joined, the warm steps found."""

import collections
import dataclasses
import gzip
import json
import pathlib

import numpy as np
from style import DATA

UNIT_KIND = {0x4D47: "MG", 0x5643: "VC"}
WRAP = 1 << 32


def lines(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    text = gzip.decompress(path.read_bytes()).decode()
    return [json.loads(x) for x in text.splitlines() if x]


def unit_deltas(rows: list[dict]) -> None:
    """Counters are cumulative and 32-bit: each row keeps its own launch's share. A
    vector core's CU_DBG is its last run, so it is taken as it stands."""
    last: dict = {}
    for row in rows:
        for u in row.get("units", ()):
            key = (u["x"], u["y"])
            prev = last.get(key, u)
            first = key not in last
            kind = UNIT_KIND.get(u["type"], "?")
            u["kind"] = kind
            u["busy_d"] = 0 if first else (u["busy"] - prev["busy"]) % WRAP
            u["retired_d"] = 0 if first else (u["retired"] - prev["retired"]) % WRAP
            mg = kind == "MG" and not first
            u["compute_d"] = (u["dbg_hi"] - prev["dbg_hi"]) % WRAP if mg else 0
            u["fill_d"] = (u["dbg_lo"] - prev["dbg_lo"]) % WRAP if mg else 0
            last[key] = u


def period(names: list[str]) -> tuple[int, int]:
    """(first warm start, step length): the longest window repeated to the run's end
    region, found over the names, the whole run when none repeats."""
    n = len(names)
    ids = {name: i for i, name in enumerate(dict.fromkeys(names))}
    code = np.array([ids[name] for name in names])
    best: list[tuple[int, int, int]] = []
    for p in range(8, n // 2):
        same = np.concatenate(([0], (code[:-p] == code[p:]).astype(int), [0]))
        edges = np.flatnonzero(np.diff(same))
        if not len(edges):
            continue
        spans = edges[1::2] - edges[::2]
        at = int(np.argmax(spans))
        best.append((int(spans[at]), p, int(edges[2 * at])))
    if not best:
        return 0, 0
    longest = max(b[0] for b in best)
    _run, p, first = next(b for b in best if b[0] >= 0.9 * longest and b[0] >= b[1])
    return first, p


@dataclasses.dataclass
class Run:
    name: str
    meta: dict
    rows: list[dict]
    kernels: dict
    profile: list[dict]
    start: int
    length: int

    @property
    def steps(self) -> list[list[dict]]:
        """Each warm step's rows."""
        if not self.length:
            return [self.rows]
        out, at = [], self.start
        while at + self.length <= len(self.rows):
            out.append(self.rows[at : at + self.length])
            at += self.length
        return out[1:] if len(out) > 2 else out

    @property
    def step(self) -> list[dict]:
        """The median warm step."""
        ordered = sorted(self.steps, key=lambda s: sum(r["cycles"] for r in s))
        return ordered[len(ordered) // 2]


def load(name: str) -> Run:
    base = DATA / name
    card = lines(base / "card.jsonl.gz")
    units = lines(base / "units.jsonl.gz")
    launches = lines(base / "launches.jsonl.gz")
    meta = json.loads((base / "meta.json").read_text("utf-8"))
    rows = []
    for i, c in enumerate(card):
        row = dict(c, index=i)
        if i < len(units):
            row["units"] = units[i]["units"]
            row["start"] = units[i]["start"]
            row["moves"] = units[i]["moves"]
        if i < len(launches):
            row["gap_ms"] = launches[i].get("gap_ms", 0.0)
            row["submit_ms"] = launches[i].get("submit_ms", 0.0)
            row["scratch"] = launches[i].get("scratch", 0)
        rows.append(row)
    unit_deltas(rows)
    start, length = period([r["name"] for r in rows])
    kernels = json.loads((base / "kernels.json").read_text("utf-8"))
    prof = base / "profile.json.gz"
    profile = json.loads(gzip.decompress(prof.read_bytes())) if prof.exists() else []
    return Run(name, meta, rows, kernels, profile, start, length)


def by_kind(rows: list[dict], key: str) -> dict:
    out: dict = collections.Counter()
    for r in rows:
        out[r["kind"]] += r.get(key, 0)
    return dict(out)


def names() -> list[str]:
    return sorted(p.name for p in DATA.glob("*.multimesh_*"))
