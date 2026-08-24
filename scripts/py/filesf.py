#!/usr/bin/env python3
"""Regenerate each library's `FILES.f` from the tree, or check they match.

    python scripts/py/filesf.py            # rewrite every manifest
    python scripts/py/filesf.py --check    # exit 1 if any has drifted

A manifest is every `.v` and `.vh` under one library, sorted, relative to
`src/`. NOTHING BUILDS FROM THESE -- `scripts/py/xsim.py` is the build list, and
that has not changed. They are an inventory, and an inventory nobody verifies
stops being one: `sysnode/FILES.f` had dropped `sysnode.v`, the library's own
top, while still being documented as the source of truth.
"""

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
#: Packages whose immediate children are libraries. `templates/`, `examples/`,
#: `reference/` and `attic/` carry no manifest and are not inventoried here.
PACKAGES = ("kohakuaccel", "kohakutpu", "kohakumpe")
SUFFIX = (".v", ".vh")


def libraries():
    for pkg in PACKAGES:
        base = SRC / pkg
        if base.is_dir():
            yield from sorted(d for d in base.iterdir() if d.is_dir())


def contents(lib: pathlib.Path) -> str:
    files = sorted(
        p.relative_to(SRC).as_posix()
        for p in lib.rglob("*")
        if p.is_file() and p.suffix in SUFFIX
    )
    return "".join(f"{f}\n" for f in files)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="report, do not write")
    args = ap.parse_args()

    drift, n = [], 0
    for lib in libraries():
        want = contents(lib)
        if not want:
            continue
        n += 1
        man = lib / "FILES.f"
        have = man.read_text(encoding="utf-8") if man.exists() else None
        rel = man.relative_to(ROOT).as_posix()
        if have == want:
            continue
        if args.check:
            if have is None:
                drift.append(f"{rel}: missing, {want.count(chr(10))} file(s)")
            else:
                a, b = set(have.split()), set(want.split())
                gone = ", ".join(sorted(b - a)) or "-"
                extra = ", ".join(sorted(a - b)) or "-"
                drift.append(f"{rel}: absent {gone}; stale {extra}")
        else:
            man.write_text(want, encoding="utf-8")
            print(f"  wrote {rel}  ({want.count(chr(10))} file(s))")

    for d in drift:
        print(f"  {d}")
    verb = "checked" if args.check else "generated"
    print(f"\n{n} manifest(s) {verb}, {len(drift)} that do not match the tree")
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
