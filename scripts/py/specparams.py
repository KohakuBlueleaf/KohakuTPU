#!/usr/bin/env python3
"""`docs/spec/parameters.md` against the RTL it claims to describe.

    python scripts/py/specparams.py            # report, non-zero on a mismatch
    python scripts/py/specparams.py --list     # every module, matched or not

A normative parameter table is the one doc a reader trusts without checking,
and it goes stale the moment a parameter is added. This reads each
`### \\`module\\` — \\`path\\`` heading, takes the table under it, and compares
name and default against the `parameter` declarations in that file.

THREE THINGS ARE REPORTED and they are not the same severity:

  MISSING   the RTL has a parameter the table does not. The table is
            incomplete; a reader configuring the module cannot see it.
  EXTRA     the table has one the RTL does not. Either it was removed and the
            doc kept it, or the name is wrong. This is the one that misleads.
  DEFAULT   both have it and disagree. The doc states a value the module does
            not have.

A default that is an expression (`GRID_HI`, `DATA_W/8`) is compared as text
after whitespace is squeezed, so `MEM_PORTS + 2` matches `MEM_PORTS  +  2`. A
row whose Default cell carries prose -- "`14` (router), `2` (agent)" -- cannot
be compared and is counted as documented-only.
"""

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = "docs/spec/parameters.md"

HEAD = re.compile(r"^###\s+`(?P<mod>[\w.]+)`\s+—\s+`(?P<path>[^`]+)`")
# A row's first cell may name SEVERAL parameters that share a description:
# `SLOTS`, `ID_W`, `MODE_W`. Taking only the first reported the rest missing.
ROW = re.compile(r"^\|(?P<names>[^|]*`\w+`[^|]*)\|(?P<type>[^|]*)\|(?P<dflt>[^|]*)\|")
NAME = re.compile(r"`(\w+)`")
# "It takes every `mag` parameter and adds:" — a table that documents the delta
# is right, and listing the base module's 34 rows again is the duplication this
# checker exists to stop.
INHERIT = re.compile(r"takes every `(?P<base>\w+)` parameter")

# `parameter integer NAME = <value>,` and the sized/typeless forms beside it.
# The default stops at `,` `)` `;` or a newline: without `;` it swallowed the
# rest of a line that packed a localparam after the parameter.
DECL = re.compile(
    r"\bparameter\s+(?:integer\s+|signed\s+|\[[^\]]*\]\s+)?"
    r"(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<dflt>[^,);\n]+)"
)


def squeeze(s):
    return " ".join(s.split())


def doc_tables(text):
    """{module: (path, {name: default_text}, base_or_None)} in heading order."""
    out, cur = {}, None
    for line in text.splitlines():
        m = HEAD.match(line)
        if m:
            cur = m.group("mod")
            out[cur] = [m.group("path"), {}, None]
            continue
        if line.startswith("## "):
            cur = None
        if cur is None:
            continue
        inh = INHERIT.search(line)
        if inh:
            out[cur][2] = inh.group("base")
        r = ROW.match(line)
        if not r:
            continue
        names = NAME.findall(r.group("names"))
        dflt = squeeze(r.group("dflt")).strip("`")
        # Several names sharing one Default cell: the cell lists them in order,
        # so no single value is that parameter's and none is comparable.
        for name in names:
            out[cur][1][name] = dflt if len(names) == 1 else ""
    return out


def rtl_params(path, mod):
    """{name: default_text} from ONE module's parameter list.

    A file may hold several modules -- `mag_dram_port.v` also defines
    `mag_dram_rr` -- so the search starts at `module <mod>` rather than at the
    top, and stops at the port list's `)(`.
    """
    src = (ROOT / path).read_text(encoding="utf-8", errors="replace")
    src = re.sub(r"//[^\n]*", "", src)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    at = re.search(rf"\bmodule\s+{re.escape(mod)}\b", src)
    if not at:
        return None
    head = src[at.end() :].split(")(", 1)[0]
    return {m.group("name"): squeeze(m.group("dflt")) for m in DECL.finditer(head)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="print every module")
    args = ap.parse_args()

    tables = doc_tables((ROOT / SPEC).read_text(encoding="utf-8"))
    bad, n_mod, n_par = [], 0, 0
    for mod, (path, doc, base) in tables.items():
        if not (ROOT / path).is_file():
            bad.append((mod, "PATH", path, "the heading names a file that is gone"))
            continue
        # Inherited names count as DOCUMENTED but are never asserted to exist:
        # "takes every `mag` parameter" says where to read them, not that this
        # module has all of them. It does not, and the exceptions are prose.
        inherited = set(tables[base][1]) if base in tables else set()
        doc = dict.fromkeys(inherited, "") | doc
        rtl = rtl_params(path, mod)
        if rtl is None:
            bad.append((mod, "PATH", path, "that file defines no such module"))
            continue
        n_mod += 1
        n_par += len(rtl)
        for name in sorted(set(rtl) - set(doc)):
            bad.append((mod, "MISSING", name, f"RTL default {rtl[name]}"))
        for name in sorted(set(doc) - set(rtl) - inherited):
            bad.append((mod, "EXTRA", name, "not a parameter of this module"))
        for name in sorted(set(doc) & set(rtl)):
            d, r = doc[name], rtl[name]
            # Uncomparable cells: empty, prose, a cross-reference, or several
            # values for several contexts (`1`, `0` — one per instantiation).
            if not d or "(" in d or d.startswith("See") or "`" in d:
                continue
            if squeeze(d).replace("'", "'") != r and d != r:
                bad.append((mod, "DEFAULT", name, f"doc {d!r} vs RTL {r!r}"))
        if args.list:
            print(f"{mod:<24} {len(doc):>3} documented, {len(rtl):>3} in {path}")

    for mod, kind, name, why in bad:
        print(f"{mod}: {kind} {name} — {why}")
    print(f"\n{n_mod} module(s), {n_par} parameter(s), {len(bad)} mismatch(es)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
