"""A recorded trace as Python: its launches, what each reads and writes, its step."""

import json
import pathlib
import re

SIGNATURE = re.compile(r"^fn \S+?\.l2\((.*)\)\s*$", re.MULTILINE)
PARAM = re.compile(r"(\w+): (\w+)\[([^\]]*)\] @(\w+)")
#: Bytes a value: an MXFP7 entry holds 128 values in 128 bytes.
VALUE_BYTES = {"f16": 2, "f32": 4, "gt4": 2, "mx7a": 1, "mx7b": 1}
FORM_KINDS = ("quant", "convert", "transpose", "copy", "move")


class Param:
    """A kernel parameter: its form and bytes."""

    def __init__(self, name: str, dtype: str, dims: str, space: str):
        self.name, self.dtype, self.space = name, dtype, space
        self.bytes = VALUE_BYTES.get(dtype, 2)
        for d in dims.split(","):
            self.bytes *= int(d)


def params(l2: str) -> dict[str, Param]:
    found = SIGNATURE.search(l2)
    if found is None:
        return {}
    return {p[0]: Param(*p) for p in PARAM.findall(found[1])}


def writes(l2: str, names) -> set[str]:
    """The parameters `l2` writes: a transfer's target, a drain's or a store's end."""
    out = set()
    for name in names:
        target = rf"^\s*(?:quant|copy|gt4|fill|transpose)\s+{name}\b"
        end = rf"->\s*{name}\b"
        if re.search(target, l2, re.MULTILINE) or re.search(end, l2):
            out.add(name)
    return out


class Launch:
    def __init__(self, index: int, row: dict, l2: str, timed: dict):
        self.index = index
        self.name, self.kind = row["name"], row["kind"]
        self.binds = row["binds"]
        self.params = params(l2)
        wrote = writes(l2, self.params)
        self.writes = {self.binds[n]: self.params[n] for n in wrote if n in self.binds}
        self.reads = {
            self.binds[n]: self.params[n]
            for n in self.params
            if n in self.binds and n not in wrote
        }
        self.timed = timed
        self.cycles = timed.get("cycles", 0.0)
        self.gap_ms = row.get("gap_ms", 0.0)
        self.submit_ms = row.get("submit_ms", 0.0)


def load(trace: pathlib.Path, machine: str) -> list[Launch]:
    timed = {}
    for line in (trace / f"timed.{machine}.jsonl").read_text("utf-8").splitlines():
        row = json.loads(line)
        timed[row["name"]] = row
    launches = []
    rows = (trace / "launches.jsonl").read_text("utf-8").splitlines()
    for i, line in enumerate(rows):
        row = json.loads(line)
        l2 = (trace / "kernels" / f"{row['name']}.l2").read_text("utf-8")
        launches.append(Launch(i, row, l2, timed[row["name"]]))
    return launches


def period(names: list[str]) -> tuple[int, int]:
    """`(start, length)` of the longest window repeated at once: the last warm step
    of a trace, whatever follows it, or (0, 0)."""
    n = len(names)
    for p in range(n // 2, 0, -1):
        for s in range(n - 2 * p, -1, -1):
            if (
                names[s] == names[s + p]
                and names[s : s + p] == names[s + p : s + 2 * p]
            ):
                return s + p, p
    return 0, 0


def step(launches: list[Launch]) -> list[Launch]:
    """The last warm step, the whole trace when no step repeats."""
    s, p = period([x.name for x in launches])
    return launches[s : s + p] if p else launches
