"""The levels from text, at the command line (docs/projects/kohakutpu/ir/text.md §4).

python -m kohakutpu.ir check FILE...          read and verify, by level
python -m kohakutpu.ir fmt FILE               the canonical text
python -m kohakutpu.ir lower FILE.l2 [-o F]   L2 -> L1 text
python -m kohakutpu.ir build FILE.l1 -o DIR   one L0 package a program
python -m kohakutpu.ir numerics -o DIR        errors vs host fp32 and MXFP7 references
    [--case NAME ...]                         (on a Verilated card: scripts/py/numerics_rtl.py)
"""

import argparse
import pathlib
import sys

from kohakuaccel.text.syntax import TextError, parse
from kohakutpu.ir import l2, l3, numerics
from kohakutpu.ir.l1 import text as l1_text
from kohakutpu.ir.l2 import text as l2_text


def level(source: str, file: str) -> str:
    stmts = parse(source, file)
    if not stmts or stmts[0].op != "level" or not stmts[0].args:
        raise TextError(
            "a text opens with `level l1`, `l2` or `l3`", 1, 1, source, file
        )
    return str(getattr(stmts[0].args[0], "text", ""))


def canonical(source: str, file: str) -> str:
    """The text read and printed: its level's canonical form."""
    match level(source, file):
        case "l1":
            return l1_text.write(l1_text.read(source, file=file))
        case "l2":
            return l2_text.write(l2_text.read(source, file=file))
        case "l3":
            return l3.write(l3.read(source, file))
    raise TextError("no such level", 1, 1, source, file)


def build(source: str, file: str, out: pathlib.Path) -> list:
    """Each program of an L1 text as a package file in `out`; the paths."""
    out.mkdir(parents=True, exist_ok=True)
    resident: dict = {}
    paths = []
    for i, prog in enumerate(l1_text.read(source, file=file)):
        path = out / f"{pathlib.Path(file).stem}.p{i}.pkg"
        path.write_bytes(prog.build(None, resident).build().to_bytes())
        paths.append(path)
    return paths


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m kohakutpu.ir")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").add_argument("files", nargs="+")
    sub.add_parser("fmt").add_argument("file")
    lo = sub.add_parser("lower")
    lo.add_argument("file")
    lo.add_argument("-o", "--out")
    bu = sub.add_parser("build")
    bu.add_argument("file")
    bu.add_argument("-o", "--out", required=True)
    nu = sub.add_parser("numerics")
    nu.add_argument("-o", "--out", required=True)
    nu.add_argument("--seed", type=int, default=0)
    nu.add_argument("--case", action="append", default=[], help="a case name to keep")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "numerics":
            rows = numerics.report(args.seed, only=args.case)
            sys.stdout.write(numerics.markdown(rows))
            for p in numerics.write(rows, pathlib.Path(args.out)):
                print(p)
        elif args.cmd == "check":
            for f in args.files:
                canonical(pathlib.Path(f).read_text(encoding="utf-8"), f)
                print(f"{f}: ok")
        elif args.cmd == "fmt":
            sys.stdout.write(
                canonical(
                    pathlib.Path(args.file).read_text(encoding="utf-8"), args.file
                )
            )
        elif args.cmd == "lower":
            text = l2.lower(
                pathlib.Path(args.file).read_text(encoding="utf-8"), file=args.file
            )
            if args.out:
                pathlib.Path(args.out).write_text(text, encoding="utf-8")
            else:
                sys.stdout.write(text)
        else:
            source = pathlib.Path(args.file).read_text(encoding="utf-8")
            for p in build(source, args.file, pathlib.Path(args.out)):
                print(p)
    except TextError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
