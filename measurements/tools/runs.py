"""A packed run as Python: one row a launch, all layers joined, the warm steps found."""

import collections
import dataclasses
import gzip
import json
import pathlib
import sys

from style import DATA, REPO, ROUND

sys.path.insert(0, str(REPO / "software" / "tests" / "perf"))
from traces import planned, repeats

UNIT_KIND = {0x4D47: "MG", 0x5643: "VC"}
WRAP = 1 << 32
#: Node clock of a profile, MHz.
CLOCK = {"low": 100, "mid": 200, "ship": 300}


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


def clock(meta: dict) -> str:
    """The profile the run ran at; `mixed` when the dies' readbacks differ, because
    idle dies change node 0's cycles. A run without readbacks has its label only."""
    profile = meta.get("profile") or "mid"
    dies = list((meta.get("clocks") or {}).values())
    return "mixed" if any(d != dies[0] for d in dies) else profile


def jit(meta: dict) -> bool:
    """Whether TinyJit replayed the run, from its arguments on a run that predates the
    `jit` key."""
    return meta["jit"] if "jit" in meta else "--no-jit" not in meta["args"]


def tag(mode: str) -> str:
    """A zoo mode's run tag in this round: `zjl` is `c1jl` in round `c1`."""
    return ROUND + mode.removeprefix("z")


def mode(name: str) -> str:
    """A run's zoo mode, whatever its round."""
    return "z" + name.split(".multimesh")[0].partition(".")[2].removeprefix(ROUND)


def run_name(model: str, zoo_mode: str, machine: str, nodes: str = "1n") -> str:
    return f"{model}.{tag(zoo_mode)}.{machine}.{nodes}"


@dataclasses.dataclass
class Run:
    name: str
    meta: dict
    rows: list[dict]
    kernels: dict
    profile: list[dict]
    start: int
    length: int
    count: int

    @property
    def steps(self) -> list[list[dict]]:
        """Each warm step's rows: the run's first repeat is its cold step."""
        if not self.length:
            return [self.rows]
        at = range(self.start, self.start + self.count * self.length, self.length)
        out = [self.rows[a : a + self.length] for a in at]
        return out[1:] if len(out) > 2 else out

    @property
    def step(self) -> list[dict]:
        """The warm step of median span on the node; of median run cycles for a run
        recorded without start stamps."""

        def span(s: list[dict]) -> int:
            if "start" not in s[0]:
                return sum(r["cycles"] for r in s)
            return s[-1]["start"] + s[-1]["cycles"] - s[0]["start"]

        return sorted(self.steps, key=span)[len(self.steps) // 2]


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
    named = [r["name"] for r in rows]
    start, count, length = repeats(named, planned(meta["args"]))
    kernels = json.loads((base / "kernels.json").read_text("utf-8"))
    prof = base / "profile.json.gz"
    profile = json.loads(gzip.decompress(prof.read_bytes())) if prof.exists() else []
    return Run(name, meta, rows, kernels, profile, start, length, count)


def by_kind(rows: list[dict], key: str) -> dict:
    out: dict = collections.Counter()
    for r in rows:
        out[r["kind"]] += r.get(key, 0)
    return dict(out)


def names() -> list[str]:
    """This round's card runs; a trace recorded offline has no card rows."""
    runs = DATA.glob("*.multimesh_*/card.jsonl.gz")
    found = sorted(p.parent.name for p in runs)
    return [n for n in found if n.partition(".")[2].startswith(ROUND)]
