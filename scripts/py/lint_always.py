"""Flag `always` blocks whose body is not wrapped in begin/end.

    python scripts/py/lint_always.py src

A bare one-statement `always` is correct until someone adds a second statement,
which then silently lands OUTSIDE the block and runs unconditionally. Exit 1
when any are found, so it can gate a build.
"""

import pathlib
import re
import sys

ALWAYS = re.compile(r"^\s*always(?:_ff|_comb|_latch)?\s*@")


def offenders(path):
    """(line number, text) for every always in `path` with no begin/end body."""
    out, lines = [], path.read_text(encoding="utf-8", errors="replace").splitlines()
    for i, line in enumerate(lines):
        if not ALWAYS.match(line) or line.lstrip().startswith("//"):
            continue
        # The body may start on this line or the next few; `begin` anywhere in
        # that window means it is wrapped.
        window = " ".join(lines[i : i + 3])
        after = window.split("@", 1)[1] if "@" in window else window
        # Strip the sensitivity list before looking for the body.
        depth, j = 0, 0
        for j, ch in enumerate(after):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
        body = after[j + 1 :]
        if not re.match(r"\s*begin\b", body):
            out.append((i + 1, line.rstrip()))
    return out


def main(argv):
    roots = argv[1:] or ["src"]
    total = 0
    for root in roots:
        for f in sorted(pathlib.Path(root).rglob("*.v")):
            for ln, text in offenders(f):
                print(f"{f}:{ln}: always without begin/end -- {text.strip()}")
                total += 1
    print(f"{total} bare always block(s)")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
