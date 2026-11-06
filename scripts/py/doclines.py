#!/usr/bin/env python3
"""Check every `file:line` a doc cites: the file exists and the line does.

    python scripts/py/doclines.py            # report, exit 1 on any finding
    python scripts/py/doclines.py --show     # every citation, resolved

A LINE NUMBER IS A CLAIM WITH A SHELF LIFE. `docpaths.py` proves the file is
still there; this proves the line is. It cannot read the sentence, so it checks
what is mechanical: the cited path names exactly one tracked file (a bare
`ops.py` fits only when one `ops.py` exists), and every line it and its
follow-on numbers (`mag.v:455, 470-472`) name is within the file and not blank.

A citation of a past tree names its revision in front, `af7c1bb:run.py:90-97`,
and is checked against the files of that revision.

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

#: Every suffix this tree cites by line: RTL, scripts, firmware, the Rust
#: stack, IR and grammar text, linker scripts, data files and other docs.
EXTS = ("v", "vh", "sv", "py", "tcl", "xdc", "c", "h", "S", "cpp", "rs", "ld")
EXTS += ("ktpu", "lark", "json", "md")
SUFFIXES = tuple(f".{e}" for e in EXTS)

#: `mag.v:455`, `noc_orchestrator.v:424-448`, `mx_tdesc.v:102`, and the same
#: with a directory in front.
CITE = re.compile(
    r"(?<![\w/.-])((?:[\w./-]*?)([\w-]+\.(?:" + "|".join(EXTS) + r")))"
    r":(\d+)(?:\s*[-–]\s*(\d+))?(?![\d\w])"
)

#: A revision in front of a citation, `af7c1bb:` or `53bf715d~1:` (its past tree).
REV = re.compile(r"(?<![\w/.-])([0-9a-f]{7,40}(?:~\d+)?):$")

#: The further lines of the same file after a citation: `mag.v:455, 470-472`,
#: `forms.py:280-286, :290-295`, `mx_cluster_cu.v:161,643`.
CONT = re.compile(r"(?:\s*,\s*:?\d+(?:\s*[-–]\s*\d+)?(?![\d\w.]))+")
NUMS = re.compile(r"(\d+)(?:\s*[-–]\s*(\d+))?")

#: A citation inside one of these is history, not a pointer at today's tree.
SKIP_LINE = re.compile(
    r"\bused to\b|\bwas at\b|\bbefore the\b|\bretired\b", re.IGNORECASE
)


def git(*args: str) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, check=True
    ).stdout


def sources(rev: str | None = None) -> list:
    """Every file a doc might cite, as repo-relative paths: tracked now, or
    in revision `rev`."""
    if rev is None:
        names = git("ls-files", "-z")
    else:
        names = git("ls-tree", "-r", "-z", "--name-only", rev)
    return [p for p in names.decode().split("\0") if p.endswith(SUFFIXES)]


def text_of(path: str, rev: str | None = None) -> list:
    """The lines of `path`, now or in revision `rev`."""
    raw = (ROOT / path).read_bytes() if rev is None else git("show", f"{rev}:{path}")
    return raw.decode("utf-8", errors="replace").splitlines()


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
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--show", action="store_true", help="print every citation")
    args = ap.parse_args()

    resolvers: dict = {}
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
                past = REV.search(raw, 0, m.start())
                rev = past.group(1) if past else None
                if rev not in resolvers:
                    try:
                        resolvers[rev] = resolver(sources(rev))
                    except subprocess.CalledProcessError:
                        resolvers[rev] = None
                resolve = resolvers[rev]
                shown = f"{rev}:{full}" if rev else full
                if resolve is None:
                    n_cite += 1
                    bad.append(f"{rel}:{at}: {shown} -> no revision {rev}")
                    continue
                path, why = resolve(full)
                if why:
                    n_cite += 1
                    bad.append(f"{rel}:{at}: {shown}:{m.group(3)} -> {why}")
                    continue
                if path is None:
                    if rev:
                        n_cite += 1
                        bad.append(f"{rel}:{at}: {shown} -> not in {rev}")
                    continue
                if (rev, path) not in lines_of:
                    lines_of[(rev, path)] = text_of(path, rev)
                body = lines_of[(rev, path)]
                spans = [(m.group(3), m.group(4))]
                more = CONT.match(raw, m.end())
                if more:
                    spans += NUMS.findall(more.group(0))
                for lo, hi in spans:
                    n_cite += 1
                    where = f"{rel}:{at}: {shown}:{lo}{'-' + hi if hi else ''}"
                    if int(hi or lo) > len(body):
                        name = path.rsplit("/", 1)[-1]
                        bad.append(f"{where} -> {name} has {len(body)} lines")
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
