"""A recorded trace as Python: its launches, what each reads and writes, its step."""

import json
import pathlib
import re

import numpy as np

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
    """`(start, length)` of the last whole step: the smallest period whose repeat
    covers the trace about as far as any period's does, so a run of many steps reads
    one step and not the longest multiple, or (0, 0)."""
    n = len(names)
    ids = {name: i for i, name in enumerate(dict.fromkeys(names))}
    code = np.array([ids[name] for name in names])
    found = []
    for p in range(1, n // 2 + 1):
        same = np.concatenate(([0], (code[:-p] == code[p:]).astype(int), [0]))
        edges = np.flatnonzero(np.diff(same))
        if not len(edges):
            continue
        spans = edges[1::2] - edges[::2]
        at = int(np.argmax(spans))
        if spans[at] >= p:
            found.append((int(spans[at]), p, int(edges[2 * at + 1])))
    if not found:
        return 0, 0
    longest = max(f[0] for f in found)
    _, p, end = next(f for f in found if f[0] >= 0.9 * longest)
    return end, p


def step(launches: list[Launch]) -> list[Launch]:
    """The last warm step, the whole trace when no step repeats."""
    s, p = period([x.name for x in launches])
    return launches[s : s + p] if p else launches
