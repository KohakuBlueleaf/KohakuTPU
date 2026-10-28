"""Constants and enums as a firmware header declares them."""

import re
from pathlib import Path

DEFINE = re.compile(
    r"^#define\s+(\w+)\s+\(?\s*(0x[0-9a-fA-F]+|\d+)[uUlL]*\s*\)?\s*(?:/\*.*)?$"
)
SHIFT = re.compile(r"^#define\s+(\w+)\s+\(\s*1[uUlL]*\s*<<\s*(\d+)\s*\)")
ENUM_ITEM = re.compile(r"^\s*(\w+)\s*=\s*(0x[0-9a-fA-F]+|\d+)\s*,?")


def defines(path: Path) -> dict:
    """{name: (value, line)} of every numeric #define in `path`."""
    out = {}
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        m = DEFINE.match(line)
        if m:
            out[m.group(1)] = (int(m.group(2), 0), n)
            continue
        s = SHIFT.match(line)
        if s:
            out[s.group(1)] = (1 << int(s.group(2)), n)
    return out


def enum(path: Path, name: str) -> dict:
    """{item: value} of `enum name { ... }` in `path`, or of the anonymous
    enum whose first item starts with `name`, and its first line."""
    lines = path.read_text(encoding="utf-8").splitlines()

    def opens(i, line):
        if re.match(rf"^\s*enum\s+{name}\b", line):
            return True
        return re.match(r"^\s*enum\s*\{", line) and lines[i + 1].strip().startswith(
            name
        )

    start = next(i for i, line in enumerate(lines) if opens(i, line))
    items = {}
    for line in lines[start + 1 :]:
        if line.strip().startswith("}"):
            break
        m = ENUM_ITEM.match(line)
        if m:
            items[m.group(1)] = int(m.group(2), 0)
    return {"items": items, "line": start + 1}
