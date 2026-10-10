"""Build a card model for speed: quiescence-gated modules and profile-guided
code layout (sim/verilator/docs/card-backend.md, "Build for speed").

    python scripts/py/card_model.py card_v9_2n
    python scripts/py/card_model.py card_v9_4n --train "python my_bench.py --build {build}"

1. `vlt.py <card> --cc ... --pgo gen`, with the old profile removed;
2. training: `card_run.py` on every compute node (boot, shared memory, the
   mover, an RV64 program), then each `--train` command, `{build}` replaced by
   the model's directory;
3. `vlt.py <card> --cc ... --pgo use`.

Code the training never ran is optimised as usual but laid out cold, so train
on what will be simulated. `--no-pgo` builds step 1 without instrumentation and
stops there.
"""

import argparse
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
HARNESS = "sim/verilator/harness/card_main.cpp"
VLT_CONFIG = "sim/verilator/card.vlt"


def vlt(card: str, root: pathlib.Path, extra: list) -> None:
    cmd = [
        sys.executable,
        str(ROOT / "scripts/py/vlt.py"),
        card,
        "--cc",
        HARNESS,
        "--keep",
        "--vlt-config",
        VLT_CONFIG,
        "--build-root",
        str(root),
        "-j",
        "32",
        *extra,
    ]
    if os.name == "nt":
        cmd.append("--native")
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)


def run(cmd: list) -> None:
    print("+", " ".join(cmd), flush=True)
    t0 = time.monotonic()
    subprocess.run(cmd, cwd=ROOT, check=True)
    print(f"  ({time.monotonic() - t0:.1f}s)", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("card", help="an xsim.py bench name, e.g. card_v9_2n")
    ap.add_argument("--build-root", default="build")
    ap.add_argument(
        "--board", default=None, help="default: multimesh_<ver> from the card name"
    )
    ap.add_argument(
        "--nodes", default=None, help="default: 0..n-1 from the card's _<n>n"
    )
    ap.add_argument(
        "--train", action="append", default=[], help="extra training command"
    )
    ap.add_argument("--no-pgo", action="store_true")
    ap.add_argument(
        "--define", "-d", action="append", default=[], help="passed to vlt.py -d"
    )
    a = ap.parse_args()
    defs = [x for d in a.define for x in ("-d", d)]

    root = pathlib.Path(a.build_root)
    root = root if root.is_absolute() else ROOT / root
    build = root / f"vlt_{a.card}"
    m = re.match(r"card_(v\w+?)_(\d+)n$", a.card)
    board = a.board or (f"multimesh_{m.group(1)}" if m else None)
    nodes = a.nodes or (",".join(str(i) for i in range(int(m.group(2)))) if m else None)
    if not a.no_pgo and (board is None or nodes is None):
        sys.exit(f"cannot derive --board/--nodes from {a.card!r}; give them")

    if a.no_pgo:
        vlt(a.card, root, defs)
        return 0

    shutil.rmtree(root / "pgo" / a.card, ignore_errors=True)
    vlt(a.card, root, ["--pgo", "gen", *defs])
    run(
        [
            sys.executable,
            str(ROOT / "scripts/py/card_run.py"),
            "--board",
            board,
            "--build",
            str(build),
            "--nodes",
            nodes,
            "--load",
            "burn",
            "--native",
        ]
    )
    for t in a.train:
        run(shlex.split(t.replace("{build}", str(build))))
    vlt(a.card, root, ["--pgo", "use", *defs])
    print(f"model at {build}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
