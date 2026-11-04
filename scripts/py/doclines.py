#!/usr/bin/env python3
"""Check every `file:line` a doc cites: the file exists and the line does.

    python scripts/py/doclines.py            # report, exit 1 on any finding
    python scripts/py/doclines.py --show     # every citation, resolved

A LINE NUMBER IS A CLAIM WITH A SHELF LIFE. `docpaths.py` proves the file is
still there; this proves the line is. It cannot read the sentence, so it checks
what is mechanical: the cited path names exactly one tracked file (a bare
`ops.py` fits only when one `ops.py` exists), and every line it and its
follow-on numbers (`mag.v:455, 470-472`) name is within the file and not blank.

The quiet half -- a line that still exists and now says something else -- only a
reader catches, and `--show` prints the cited line so they can do it quickly.
"""

import argparse
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCS = (ROOT / "docs", ROOT / "docs-web" / "src")

#: `mag.v:455`, `noc_orchestrator.v:424-448`, `mx_tdesc.v:102`, and the same
#: with a directory in front. The suffix list is what this tree actually cites.
CITE = re.compile(
    r"(?<![\w/.-])((?:[\w./-]*?)([\w-]+\.(?:v|vh|py|tcl|xdc|sv)))"
    r":(\d+)(?:\s*[-–]\s*(\d+))?(?![\d\w])"
)

#: The further lines of the same file after a citation: `mag.v:455, 470-472`,
#: `forms.py:280-286, :290-295`, `mx_cluster_cu.v:161,643`.
CONT = re.compile(r"(?:\s*,\s*:?\d+(?:\s*[-–]\s*\d+)?(?![\d\w.]))+")
NUMS = re.compile(r"(\d+)(?:\s*[-–]\s*(\d+))?")

#: A citation inside one of these is history, not a pointer at today's tree.
SKIP_LINE = re.compile(
    r"\bused to\b|\bwas at\b|\bbefore the\b|\bretired\b", re.IGNORECASE
)


SUFFIXES = (".v", ".vh", ".py", ".tcl", ".xdc", ".sv")


def sources() -> list:
    """Every tracked file a doc might cite, as repo-relative paths."""
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout.decode()
    return [p for p in out.split("\0") if p.endswith(SUFFIXES)]


def resolver(tracked: list):
    """`resolve(cited)` -> (path, None) or (None, why).

    A cited path names the one tracked file it is a suffix of: `ops.py` when
    only one `ops.py` exists, `dsl/ops.py` or the full path otherwise. A citation
    that fits several files is a finding: the doc has to say which one.
    """
    exact = set(tracked)
    by_name: dict = {}
    for p in tracked:
        by_name.setdefault(p.rsplit("/", 1)[-1], []).append(p)

    def resolve(cited: str):
        cited = cited.strip("./")
        if cited in exact:
            return cited, None
        name = cited.rsplit("/", 1)[-1]
        fits = [p for p in by_name.get(name, []) if ("/" + p).endswith("/" + cited)]
        if len(fits) == 1:
            return fits[0], None
        if not fits:
            return None, None  # docpaths.py owns "does the file exist"
        return None, f"names {len(fits)} files ({', '.join(fits[:3])}...)"

    return resolve


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

    resolve = resolver(sources())
    lines_of: dict = {}
    n_cite = 0
    bad: list = []

    for doc in docs():
        text = doc.read_text(encoding="utf-8", errors="replace")
        rel = doc.relative_to(ROOT).as_posix()
        for at, raw in enumerate(text.splitlines(), 1):
            if SKIP_LINE.search(raw):
                continue
            for m in CITE.finditer(raw):
                full = m.group(1)
                path, why = resolve(full)
                if why:
                    n_cite += 1
                    bad.append(f"{rel}:{at}: {full}:{m.group(3)} -> {why}")
                    continue
                if path is None:
                    continue
                src = ROOT / path
                if src not in lines_of:
                    lines_of[src] = src.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()
                body = lines_of[src]
                spans = [(m.group(3), m.group(4))]
                more = CONT.match(raw, m.end())
                if more:
                    spans += NUMS.findall(more.group(0))
                for lo, hi in spans:
                    n_cite += 1
                    where = f"{rel}:{at}: {full}:{lo}{'-' + hi if hi else ''}"
                    if int(hi or lo) > len(body):
                        bad.append(f"{where} -> {src.name} has {len(body)} lines")
                    elif not body[int(lo) - 1].strip():
                        bad.append(f"{where} -> line {lo} is blank")
                    elif args.show:
                        print(f"  {where}  {body[int(lo) - 1].strip()}")

    for b in bad:
        print(f"  {b}")
    print(f"\n{n_cite} line citation(s), {len(bad)} that do not resolve")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
