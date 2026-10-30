"""Foreground A/B OS hot-path measurements; output images and JSON stay in --out."""

import argparse
import json
import pathlib
import sys

TOOLS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from boot import load, parse_flags
from build import HOT as KNOBS
from build import build
from run_core import run


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=pathlib.Path, required=True)
    ap.add_argument("--model", type=pathlib.Path, required=True)
    ap.add_argument("--board", default="core2")
    ap.add_argument("--cmd", default="bench 8")
    ap.add_argument("--flags", default="")
    ap.add_argument("--variants", nargs="*", type=int, default=list(range(11)))
    ap.add_argument("--profile", action="store_true")
    ap.add_argument("--build-only", action="store_true")
    ap.add_argument("-D", dest="extra", action="append", default=[])
    a = ap.parse_args()
    for n in a.variants:
        out = a.out / str(n)
        out.mkdir(parents=True, exist_ok=True)
        defines = [f"-D{k}" for k in (*KNOBS[:n], *a.extra)]
        if a.profile:
            defines.append("-DKOS_PROF")
        sizes = build("kos", out, defines)
        if a.build_only:
            print(json.dumps(dict(defines=defines, sizes=sizes)))
            continue
        result = run(
            load(a.board),
            a.model,
            out / "kos.elf",
            a.cmd,
            parse_flags(a.flags),
            max_cycles=8_000_000,
            log=str(out / "console.txt"),
        )
        result.update(sizes=sizes, defines=defines, board=a.board, cmd=a.cmd)
        (out / "result.json").write_text(json.dumps(result, indent=2))
        print(
            json.dumps(
                {k: result[k] for k in ("defines", "sizes", "rc", "fails", "measures")}
            )
        )
        if result["rc"] or result["fails"]:
            print(result["output"])
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
