#!/usr/bin/env python3
"""The framework must not instantiate a project's module. This measures it.

    python scripts/py/deps.py            # report, exit 1 on a new edge
    python scripts/py/deps.py --list     # every edge, allowed ones included

`src/kohakuaccel/` is the framework and `src/kohakutpu/`, `src/kohakumpe/` are
projects. A framework module that names a project's is not reusable -- it is one
accelerator with its parts in two directories -- and the failure is quiet,
because the build lists carry both trees anyway.

What it permits is a SLOT, named in `ALLOWED` with the reason. Everything else
fails the run. The check reads instantiations rather than build lists, so adding
a file to a list cannot hide one.
"""

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]

MODULE = re.compile(r"^\s*module\s+([A-Za-z_]\w*)", re.MULTILINE)
COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
#: `name #(` or `name inst (` with nothing but indent before it.
INST = re.compile(r"^[ \t]*([A-Za-z_]\w*)\s*(?:#\s*\(|[A-Za-z_]\w*\s*\()", re.MULTILINE)

FRAMEWORK = ("src", "kohakuaccel")
PROJECTS = {("src", "kohakutpu"), ("src", "kohakumpe")}
#: Trees a framework module may name without it being a project dependency.
NEUTRAL = (("src", "templates"), ("src", "reference"), ("src", "attic"), ("sim",))
#: Standalone libraries: anything may instantiate them; they instantiate
#: nothing outside their own tree. Checked in both directions.
LIBRARIES = {("src", "kohakutransmit")}

#: Slots: named behind a parameter defaulting to 0, so nothing elaborates one.
#: `src/templates/` also has `xform_bank`, so NEUTRAL eats it -- two print.
ALLOWED = {
    # `mag_xform` names one bank; a project or `src/templates/transform/`
    # supplies it. That is what a slot IS.
    "xform_bank",
    # The SIMD extension, at `SIMD_EN`. KohakuMPE ships one; the framework's
    # rv32 is the base core bit for bit without it.
    "khs_unit",
    "khs_scalar_decode",
}

KEYWORDS = {
    "module",
    "endmodule",
    "if",
    "else",
    "for",
    "while",
    "case",
    "casez",
    "casex",
    "endcase",
    "begin",
    "end",
    "always",
    "assign",
    "initial",
    "task",
    "function",
    "endfunction",
    "endtask",
    "generate",
    "endgenerate",
    "wire",
    "reg",
    "input",
    "output",
    "inout",
    "parameter",
    "localparam",
    "integer",
    "genvar",
    "return",
    "posedge",
    "negedge",
    "or",
    "and",
    "not",
    "repeat",
    "forever",
    "wait",
    "disable",
    "default",
    "signed",
    "unsigned",
    "real",
}


def sources():
    for p in sorted((ROOT / "src").rglob("*.v")):
        if {"build", "node_modules"} & set(p.parts):
            continue
        yield p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list", action="store_true", help="print allowed edges too")
    args = ap.parse_args()

    owner, bodies = {}, {}
    for p in sources():
        clean = COMMENT.sub("", p.read_text(encoding="utf-8", errors="replace"))
        bodies[p] = clean
        for name in MODULE.findall(clean):
            owner.setdefault(name, set()).add(p)

    bad, allowed = [], []
    for p, clean in bodies.items():
        parts = p.relative_to(ROOT).parts
        home = parts[:2]
        if home in LIBRARIES:
            # A library names only itself.
            for name in sorted(set(INST.findall(clean))):
                if name in KEYWORDS or name not in owner:
                    continue
                if any(q.relative_to(ROOT).parts[:2] == home for q in owner[name]):
                    continue
                where = sorted(q.relative_to(ROOT).as_posix() for q in owner[name])
                bad.append((p.relative_to(ROOT).as_posix(), name, where))
            continue
        if home != FRAMEWORK:
            continue
        for name in sorted(set(INST.findall(clean))):
            if name in KEYWORDS or name not in owner:
                continue
            # A module the framework also defines is the framework's, whatever
            # else in the tree happens to carry a copy of that name.
            if any(q.relative_to(ROOT).parts[:2] == FRAMEWORK for q in owner[name]):
                continue
            homes = {q.relative_to(ROOT).parts[:2] for q in owner[name]}
            if not homes & PROJECTS:
                continue
            if any(h in NEUTRAL for h in homes):
                continue
            where = sorted(q.relative_to(ROOT).as_posix() for q in owner[name])
            row = (p.relative_to(ROOT).as_posix(), name, where)
            (allowed if name in ALLOWED else bad).append(row)

    for user, name, where in sorted(bad):
        print(f"  {user}: instantiates {name} from {', '.join(where)}")
    if args.list:
        for user, name, where in sorted(allowed):
            print(f"  ALLOWED  {user}: {name} from {', '.join(where)}")

    print(
        f"\n{len(bodies)} framework file(s) read, {len(bad)} project "
        f"dependenc{'y' if len(bad) == 1 else 'ies'}, {len(allowed)} allowed"
    )
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
