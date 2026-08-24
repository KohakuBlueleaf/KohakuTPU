#!/usr/bin/env python3
"""Every repo path a document cites, checked against the tree.

    python scripts/py/docpaths.py            # docs/, docs-web/, *.md
    python scripts/py/docpaths.py docs-web

A doc naming a moved file is the defect a rename produces by the dozen: `mas/`
became `sysnode/` and `src/kohakunoc/` became `src/kohakuaccel/noc/`, both
leaving citations behind in pages that still read as current. Link checking
does not see them -- they are prose, `code` spans and Vue string literals.

A citation is deliberately only a token starting with a top-level directory
name and ending in a known extension or a `/`. Guessing wider produces noise
that gets the check ignored.
"""

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]

DEFAULTS = ("docs", "docs-web/src", "README.md", "CLAUDE.md", "CONTRIBUTING.md")

# Where a citation may point. A path outside these is not ours to check.
TOPS = ("src", "tests", "scripts", "compiler", "driver", "boards", "docs")

EXTS = ("v", "vh", "sv", "py", "tcl", "ps1", "md", "json", "txt", "xdc", "f")

CITE = re.compile(
    r"(?<![\w/.-])(?P<p>(?:" + "|".join(TOPS) + r")/[\w./-]*?"
    r"(?:\.(?:" + "|".join(EXTS) + r")|/))(?![\w.-])"
)

# `[text](../sysnode/)` — a RELATIVE markdown link, resolved against the file's
# own directory. Eight of these pointed at `../mas/`, a directory two renames
# ago; a repo-rooted path check cannot see them because they name no top-level
# directory at all. External links and in-page anchors are skipped.
LINK = re.compile(r"\]\((?P<t>[^)\s#][^)\s]*)\)")

# Read as text; anything else is an asset.
TEXTY = (".md", ".vue", ".js", ".ts", ".py", ".json", ".txt", ".html", ".css")

# A line may name a dead path ON PURPOSE -- "the retired src/ktpu package",
# "directories that no longer exist" -- and reporting those is how a checker
# gets ignored. A line saying so in one of these words is taken at its word.
# It is a heuristic, and the cost of it being wrong is one missed rename.
DELIBERATE = re.compile(
    r"no longer exist|retired|delet|removed|left behind|pre-rename"
    r"|used to|survives only|generated into|is generated|stale",
    re.IGNORECASE,
)


# `src/examples/NAME/NAME_cu.v` is a shape to fill in, not a file. An all-caps
# or angle-bracketed segment is the tree's way of writing one.
PLACEHOLDER = re.compile(r"(?:^|/)(?:[A-Z][A-Z0-9_]*|<[^/>]+>)(?:$|[/_.])")


def cited(path):
    """(line, cite, resolved) for every citation in one file.

    `cite` is what the doc says and `resolved` what it means: a repo-rooted
    path resolves against ROOT, a relative markdown link against the file's own
    directory. Lines that deliberately name a dead path are skipped.
    """
    out = []
    here = path.parent
    text = path.read_text(encoding="utf-8", errors="replace")
    for i, line in enumerate(text.splitlines(), 1):
        if DELIBERATE.search(line):
            continue
        for m in CITE.finditer(line):
            if PLACEHOLDER.search(m.group("p")):
                continue
            out.append((i, m.group("p"), ROOT / m.group("p")))
        if path.suffix != ".md":
            continue
        for m in LINK.finditer(line):
            t = m.group("t").split("#", 1)[0]
            if not t or "://" in t or t.startswith(("/", "mailto:")):
                continue
            out.append((i, t, here / t))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*", default=list(DEFAULTS))
    ap.add_argument("--list", action="store_true", help="print every citation")
    args = ap.parse_args()

    files = []
    for p in args.paths:
        q = ROOT / p
        if q.is_dir():
            files += [f for f in sorted(q.rglob("*")) if f.suffix in TEXTY]
        elif q.is_file():
            files.append(q)

    n_cites, bad = 0, []
    for f in files:
        for line, cite, target in cited(f):
            n_cites += 1
            if args.list:
                print(f"{f.relative_to(ROOT).as_posix()}:{line}: {cite}")
            if not target.exists():
                bad.append((f.relative_to(ROOT).as_posix(), line, cite))

    for rel, line, cite in bad:
        print(f"{rel}:{line}: {cite}")
    print(f"\n{n_cites} citation(s) in {len(files)} file(s), {len(bad)} dangling")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
