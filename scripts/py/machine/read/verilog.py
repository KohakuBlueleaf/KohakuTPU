"""Parameter values as an RTL file declares them, under a define set."""

import re
from functools import cache
from pathlib import Path

PARAM = re.compile(
    r"^\s*(?:parameter|localparam)\s+(?:integer\s+|\[[^\]]*\]\s*)?(\w+)\s*=\s*([^,;/]+)"
)
DIRECTIVE = re.compile(r"^\s*`(ifdef|ifndef|else|endif)\b\s*(\w+)?")


def _value(text: str, defines: dict):
    text = text.strip()
    if text.startswith("`"):
        return _value(str(defines[text[1:]]), defines)
    if text.startswith('"'):
        return text.strip('"')
    sized = re.fullmatch(r"\d*'([hdb])([0-9a-fA-F_]+)", text)
    if sized:
        return int(
            sized.group(2).replace("_", ""), {"h": 16, "d": 10, "b": 2}[sized.group(1)]
        )
    return int(text, 0)


@cache
def _lines(path: Path) -> tuple:
    return tuple(path.read_text(encoding="utf-8").splitlines())


def params(path: Path, defines: dict) -> dict:
    """{name: (value, line)} of every parameter `path` declares, taking the
    branch of each `ifdef` the define set selects."""
    live, stack, out = True, [], {}
    for n, line in enumerate(_lines(path), 1):
        d = DIRECTIVE.match(line)
        if d:
            kind, name = d.groups()
            if kind in ("ifdef", "ifndef"):
                stack.append(live)
                live = live and ((name in defines) == (kind == "ifdef"))
            elif kind == "else":
                live = stack[-1] and not live
            else:
                live = stack.pop()
            continue
        m = PARAM.match(line) if live else None
        if m and m.group(1) not in out:
            try:
                out[m.group(1)] = (_value(m.group(2), defines), n)
            except (KeyError, ValueError):
                continue
    return out
