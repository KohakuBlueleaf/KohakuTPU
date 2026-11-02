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
#: Two windows are one step when this share of their launches name one kernel: a
#: trainer's steps differ by the kernels its first step and its optimizer specialise.
SAME_STEP = 0.75


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
    """The parameters `l2` writes: the target of any `op x <- ...`, a drain's end."""
    out = set()
    for name in names:
        target = rf"^\s*[a-z]\w*\s+{name}\b.*<-"
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


def matches(code: np.ndarray, p: int) -> np.ndarray:
    """At each start i: how many of `code[i : i + p]` equal `code[i + p : i + 2 * p]`."""
    same = np.cumsum(np.concatenate(([0], code[:-p] == code[p:])))
    return same[p:] - same[:-p]


def share(code: np.ndarray, p: int) -> float:
    """The share of `code` that equals itself `p` later."""
    return float(np.mean(code[:-p] == code[p:]))


def tail(code: np.ndarray) -> tuple[int, int]:
    """`(end, length)` of the repeat at the trace's end, or (0, 0): the longest window
    that follows a copy of itself with at most 1.5 of its lengths after it. The upper
    half of the periods that repeat holds a whole number of steps, the best match
    there; when that window is itself several steps, the shortest period that holds
    the pair as well as the pair does."""
    n = len(code)
    periods = range(1, n // 2 + 1)
    late = {p: matches(code, p)[max(0, n - 7 * p // 2) :] for p in periods}
    held = {p: found.max() / p for p, found in late.items()}
    repeating = [p for p, v in held.items() if v >= SAME_STEP]
    if not repeating:
        return 0, 0
    longest = repeating[-1]
    p = max((q for q in repeating if 2 * q > longest), key=lambda q: (held[q], q))
    from_end = int(np.argmax(late[p][::-1]))
    end = n - from_end
    pair = code[end - 2 * p : end]
    for q in range(1, p):
        if p % q == 0 and share(pair, q) >= held[p]:
            p = q
            break
    return end, p


def coded(names: list[str]) -> np.ndarray:
    ids = {name: i for i, name in enumerate(dict.fromkeys(names))}
    return np.array([ids[name] for name in names])


def planned(args: list[str]) -> int | None:
    """The steps a run was asked for: its last `--steps` or `--inputs`."""
    given = [args[i + 1] for i, a in enumerate(args) if a in ("--steps", "--inputs")]
    return int(given[-1]) if given else None


def whole(names: list[str], asked: int | None) -> tuple[int, int, int]:
    """`(first, end, length)` of the repeating tail, from where its launches start to
    equal those a period later. More repeats than the run's `asked` steps are blocks
    inside one step: with no second copy of it, the whole tail is the step."""
    code = coded(names)
    end, p = tail(code)
    if not p:
        return 0, 0, 0
    first = end - p
    while first and code[first - 1] == code[first - 1 + p]:
        first -= 1
    return first, end, end - first if asked and (end - first) // p > asked else p


def repeats(names: list[str], asked: int | None = None) -> tuple[int, int, int]:
    """`(first, steps, length)`: the run's identical steps, or (0, 0, 0)."""
    first, end, p = whole(names, asked)
    return first, (end - first) // p if p else 0, p


def period(names: list[str], asked: int | None = None) -> tuple[int, int]:
    """`(start, length)` of the last whole step, or (0, 0)."""
    _, end, p = whole(names, asked)
    return end - p, p


def step(launches: list[Launch], asked: int | None = None) -> list[Launch]:
    """The last warm step, the whole trace when no step repeats."""
    s, p = period([x.name for x in launches], asked)
    return launches[s : s + p] if p else launches
