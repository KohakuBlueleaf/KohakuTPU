#!/usr/bin/env python3
"""Check every `file:line` a doc cites: the file exists and the line does.

    python scripts/py/doclines.py            # report, exit 1 on any finding
    python scripts/py/doclines.py --show     # every citation, resolved

A LINE NUMBER IS A CLAIM WITH A SHELF LIFE. `docpaths.py` proves the file is
still there; nothing proved the line is. This cannot read the sentence, so it
checks what is mechanical: the line is within the file, and it is not blank.

The quiet half -- a line that still exists and now says something else -- only a
reader catches, and `--show` prints the cited line so they can do it quickly.
"""

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCS = (ROOT / "docs", ROOT / "docs-web" / "src")

#: `mag.v:455`, `noc_orchestrator.v:424-448`, `mx_tdesc.v:102`, and the same
#: with a directory in front. The suffix list is what this tree actually cites.
CITE = re.compile(
    r"(?<![\w/.-])((?:[\w./-]*?)([\w-]+\.(?:v|vh|py|tcl|xdc|sv)))"
    r":(\d+)(?:\s*[-–]\s*(\d+))?(?![\d\w])"
)

#: A citation inside one of these is history, not a pointer at today's tree.
SKIP_LINE = re.compile(
    r"\bused to\b|\bwas at\b|\bbefore the\b|\bretired\b", re.IGNORECASE
)


def sources() -> dict:
    """Every file a doc might cite, by basename and by repo-relative path."""
    out: dict = {}
    for p in ROOT.rglob("*"):
        if not p.is_file() or p.suffix.lstrip(".") not in {
            "v",
            "vh",
            "py",
            "tcl",
            "xdc",
            "sv",
        }:
            continue
        parts = set(p.parts)
        if parts & {"build", "node_modules", ".git", ".venv", "dist"}:
            continue
        rel = p.relative_to(ROOT).as_posix()
        out[rel] = p
        # A bare basename is ambiguous when two trees hold the same name; the
        # live source wins, because that is what a doc means by it.
        key = p.name
        if key not in out or "attic" in out[key].parts or "reference" in out[key].parts:
            out[key] = p
    return out


def docs() -> list:
    out = []
    for root in DOCS:
        if root.exists():
            out += [p for p in root.rglob("*") if p.suffix in {".md", ".vue"}]
    return sorted(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--show", action="store_true", help="print every citation")
    args = ap.parse_args()

    known = sources()
    lines_of: dict = {}
    n_cite = 0
    bad: list = []

    for doc in docs():
        text = doc.read_text(encoding="utf-8", errors="replace")
        for at, raw in enumerate(text.splitlines(), 1):
            if SKIP_LINE.search(raw):
                continue
            for full, base, lo, hi in CITE.findall(raw):
                src = known.get(full.strip("./")) or known.get(base)
                if src is None:
                    continue  # docpaths.py owns "does the file exist"
                n_cite += 1
                if src not in lines_of:
                    lines_of[src] = src.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()
                body = lines_of[src]
                want = int(hi or lo)
                rel = doc.relative_to(ROOT).as_posix()
                if want > len(body):
                    bad.append(
                        f"{rel}:{at}: {full}:{lo}"
                        f"{'-' + hi if hi else ''} -> {src.name} has "
                        f"{len(body)} lines"
                    )
                elif not body[int(lo) - 1].strip():
                    bad.append(f"{rel}:{at}: {full}:{lo} -> that line is blank")
                elif args.show:
                    print(f"  {rel}:{at}  {full}:{lo}  {body[int(lo) - 1].strip()}")

    for b in bad:
        print(f"  {b}")
    print(f"\n{n_cite} line citation(s), {len(bad)} that do not resolve")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
