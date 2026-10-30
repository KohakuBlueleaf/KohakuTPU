#!/usr/bin/env python3
"""Check every `page.md#heading` a doc cites: the heading is still there.

    python scripts/py/docanchors.py            # report, exit 1 on any finding
    python scripts/py/docanchors.py --list     # every anchor a page defines

`docpaths.py` strips the fragment before it resolves anything, so a link into a
heading that has been renamed or deleted passes every check while landing the
reader at the top of the page with no sign anything is wrong. That is the whole
gap this closes.

Anchors are matched the way GitHub and the site both generate them: lowercase,
punctuation dropped, spaces to hyphens. An `<a id=>` or `{#explicit}` is taken
as declared.
"""

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCS = (ROOT / "docs",)

HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
#: `## Heading {#explicit}` and a bare `<a id="x">`/`<a name="x">`.
EXPLICIT = re.compile(r"\{#([\w-]+)\}")
HTML_ID = re.compile(r"<a\s+(?:id|name)=[\"']([\w-]+)[\"']", re.IGNORECASE)
#: A markdown link with a fragment, relative or same-page.
LINK = re.compile(r"\]\((?P<t>[^)\s]*?)#(?P<frag>[\w.-]+)\)")
#: Fenced code holds example links that are not citations.
FENCE = re.compile(r"^\s*(```|~~~)")


def slug(text: str) -> str:
    """GitHub's heading slug: strip markup, lowercase, spaces to hyphens."""
    t = re.sub(r"`([^`]*)`", r"\1", text)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    # Not `_`: it is markdown emphasis AND half the identifiers here, and
    # GitHub keeps it. Stripping it made `mm_mover` slug as `mmmover`.
    t = re.sub(r"[*~]", "", t)
    t = EXPLICIT.sub("", t).strip().lower()
    t = re.sub(r"[^\w\s-]", "", t)
    # One hyphen per SPACE, not per run -- a dropped em-dash leaves two spaces
    # and GitHub renders `--`. Collapsing called 6 live anchors dangling.
    return re.sub(r"\s", "-", t)


def anchors_of(path: pathlib.Path, text: str) -> set:
    out = {slug(h) for _, h in HEADING.findall(text)}
    out |= set(EXPLICIT.findall(text)) | set(HTML_ID.findall(text))
    return out


def strip_fences(text: str) -> str:
    keep, inside = [], False
    for line in text.splitlines():
        if FENCE.match(line):
            inside = not inside
            keep.append("")
            continue
        keep.append("" if inside else line)
    return "\n".join(keep)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list", action="store_true", help="print anchors per page")
    args = ap.parse_args()

    pages = sorted(p for root in DOCS if root.exists() for p in root.rglob("*.md"))
    raw = {p: p.read_text(encoding="utf-8", errors="replace") for p in pages}
    have = {p: anchors_of(p, t) for p, t in raw.items()}

    if args.list:
        for p in pages:
            print(f"  {p.relative_to(ROOT).as_posix()}")
            for a in sorted(have[p]):
                print(f"      #{a}")

    n, bad = 0, []
    for p in pages:
        for at, line in enumerate(strip_fences(raw[p]).splitlines(), 1):
            for target, frag in LINK.findall(line):
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                if target == "":
                    dest = p
                else:
                    dest = (p.parent / target).resolve()
                    if dest.is_dir():
                        dest = dest / "README.md"
                if dest not in have:
                    continue  # docpaths.py owns "does the file exist"
                n += 1
                if frag.lower() not in have[dest]:
                    rel = p.relative_to(ROOT).as_posix()
                    to = dest.relative_to(ROOT).as_posix()
                    bad.append(f"{rel}:{at}: #{frag} is not a heading in {to}")

    for b in bad:
        print(f"  {b}")
    print(f"\n{n} anchor citation(s), {len(bad)} that do not resolve")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
