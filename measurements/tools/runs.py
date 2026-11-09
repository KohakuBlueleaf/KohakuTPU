"""A packed run as Python: one row a launch, all layers joined, the warm steps found."""

import collections
import dataclasses
import gzip
import itertools
import json
import pathlib
import sys

from style import DATA, REPO, ROUND

sys.path.insert(0, str(REPO / "software" / "tests" / "perf"))
import mfu as MFU  # noqa: F401 -- the figures' import, past the path above
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
    def launched(self) -> list[dict]:
        """The rows of single kernels: a replay list runs a recorded step's kernels
        as one row, with no cycles of its own kernels."""
        return [r for r in self.rows if r["kind"] != "replay"]

    @property
    def steps(self) -> list[list[dict]]:
        """Each warm step's kernel rows: the run's first repeat is its cold step."""
        rows = self.launched
        if not self.length:
            return [rows]
        at = range(self.start, self.start + self.count * self.length, self.length)
        out = [rows[a : a + self.length] for a in at]
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

    @property
    def cadence(self) -> int:
        """Median node cycles from a warm step's first launch to the next's: everything
        the node waits for, host included, on capture and replay steps alike. A replayed
        step is its lists, a step's first list holding one tag."""
        lists = [r for r in self.rows if r["kind"] == "replay" and "start" in r]
        if lists:
            first = [r for r in lists if r["item_tag"] == lists[0]["item_tag"]]
        else:
            first = [s[0] for s in self.steps]
        gaps = [b["start"] - a["start"] for a, b in itertools.pairwise(first)]
        return sorted(gaps)[len(gaps) // 2]


def load(name: str) -> Run:
    base = DATA / name
    card = lines(base / "card.jsonl.gz")
    units = lines(base / "units.jsonl.gz")
    launches = lines(base / "launches.jsonl.gz")
    meta = json.loads((base / "meta.json").read_text("utf-8"))
    if meta.get("schema", 0) >= 2:
        raise ValueError(
            "schema-2 traces use explicit input/sysnode joins; use build_round's matrix report, not legacy positional zoo figures"
        )
    rows: list[dict] = []
    at = 0
    # A run recorded with event identities joins its host event by `seq`: one
    # event may publish several card rows (a replay's lists), and its host time
    # goes on the first. Older runs join by position.
    by_seq = {e["seq"]: e for e in launches if "seq" in e}
    joined: set = set()
    for i, c in enumerate(card):
        row: dict = dict(c, index=i)
        # A replay list is one card row and one ring slot per launch in it; a
        # replayed single run (list_index 0) takes one slot.
        slots = max(1, c["list_index"]) if c["kind"] == "replay" else 1
        taken = units[at : at + slots]
        at += len(taken)
        if taken:
            row["units"] = [u for t in taken for u in t["units"]]
            row["start"] = taken[0]["start"]
            row["moves"] = sum(t["moves"] for t in taken)
        if by_seq:
            event = by_seq.get(c.get("seq")) or {}
            first = bool(event) and c["seq"] not in joined
            joined.add(c.get("seq"))
            row["gap_ms"] = event.get("gap_ms", 0.0) if first else 0.0
            row["submit_ms"] = event.get("submit_ms", 0.0) if first else 0.0
            row["scratch"] = event.get("scratch", 0)
        elif i < len(launches):
            row["gap_ms"] = launches[i].get("gap_ms", 0.0)
            row["submit_ms"] = launches[i].get("submit_ms", 0.0)
            row["scratch"] = launches[i].get("scratch", 0)
        rows.append(row)
    unit_deltas(rows)
    named = [r["name"] for r in rows if r["kind"] != "replay"]
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
