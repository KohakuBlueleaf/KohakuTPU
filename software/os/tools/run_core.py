"""Run an image on a core-only cluster model (sim/build.py) and collect it.

    python software/os/tools/run_core.py --board core4s --model ~/kos/build/core4s \\
        --image build/os/m0.elf --cmd "lat ring sc amo" [--flags poll] [--json out.json]

Every node boots the same image with its own boot table and its copy of the
image's user part at its private base (tools/image.py). Lines "@ name value"
on any node's console are measurements, gathered per node into the JSON.
Exit status is the model's: 0 when every node exited 0 (and printed --expect).
"""

import argparse
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import time

import klog
from boot import BOOT_PA, load, parse_flags, regions, tables
from image import private_part

MEASURE = re.compile(r"^\[(\d+)\] @ (\S+) (-?\d+)$")


def run(
    board: dict,
    model: pathlib.Path,
    image: pathlib.Path,
    cmd: str = "",
    flags: int = 0,
    tick: int = 0,
    max_cycles: int = 50_000_000,
    expect: str | None = None,
    extra: list[str] | None = None,
    log: str | None = None,
    boots: int = 1,
    hold: list[int] | None = None,
) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        args = [
            str(model / "obj_dir" / "vsim"),
            "--elf",
            str(image),
            "--max-cycles",
            str(max_cycles),
        ]
        private = private_part(image)
        args += ["--boots", str(boots)]
        for n in hold or []:
            args += ["--hold", str(n)]
        for b in range(boots):
            for i, t in enumerate(tables(board, flags, tick, cmd)):
                p = pathlib.Path(tmp) / f"boot{b}_{i}.bin"
                p.write_bytes(t)
                args += ["--blob-at", str(b), f"{i}:{BOOT_PA:#x}:{p}"]
        logs = []
        for i in range(board["nodes"]):
            base, size = regions(board, i)[0][:2]
            logs.append(pathlib.Path(tmp) / f"log{i}.bin")
            args += [
                "--dump",
                f"{i}:{base + klog.offset(size):#x}:{klog.LOG}:{logs[i]}",
            ]
            for k, (off, data) in enumerate(private):
                q = pathlib.Path(tmp) / f"uimg{i}_{k}.bin"
                q.write_bytes(data)
                args += ["--blob", f"{i}:{base + off:#x}:{q}"]
            if expect:
                args += ["--expect", str(i), expect]
        args += extra or []
        # the model's lines land in a file as they come, so a long run can be watched
        live = pathlib.Path(log) if log else pathlib.Path(tmp) / "out.txt"
        t0 = time.monotonic()
        with live.open("w") as f:
            p = subprocess.run(args, stdout=f, stderr=subprocess.STDOUT, check=False)
        wall = time.monotonic() - t0
        out = live.read_text(errors="replace")
        nodes = [klog.parse(q.read_bytes()) for q in logs if q.exists()]
    meas: dict[str, dict[str, int]] = {}
    for line in out.splitlines():
        m = MEASURE.match(line)
        if m:
            meas.setdefault(m[1], {})[m[2]] = int(m[3])
    fails = [ln for ln in out.splitlines() if "FAIL" in ln]
    return {
        "rc": p.returncode,
        "wall": wall,
        "measures": meas,
        "fails": fails,
        "nodes": nodes,
        "output": out,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--cmd", default="")
    ap.add_argument("--flags", default="")
    ap.add_argument("--tick", type=int, default=0)
    ap.add_argument("--max-cycles", type=int, default=50_000_000)
    ap.add_argument("--expect")
    ap.add_argument("--json")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--log", help="the model's output, written while it runs")
    ap.add_argument(
        "--boots",
        type=int,
        default=1,
        help="run the image this many times in one model, a fresh boot table each time",
    )
    ap.add_argument(
        "--hold", type=int, action="append", help="leave this node in reset"
    )
    a = ap.parse_args()
    r = run(
        load(a.board),
        pathlib.Path(a.model),
        pathlib.Path(a.image),
        a.cmd,
        parse_flags(a.flags),
        a.tick,
        a.max_cycles,
        a.expect,
        ["--quiet"] if a.quiet else None,
        a.log,
        a.boots,
        a.hold,
    )
    print(r["output"])
    for i, nd in enumerate(r["nodes"]):
        print(f"[{i}] log: {klog.describe(nd)}")
    print(f"run_core: rc {r['rc']} in {r['wall']:.1f}s, {len(r['fails'])} FAIL lines")
    if a.json:
        doc = {k: v for k, v in r.items() if k != "output"}
        doc.update(board=a.board, image=a.image, cmd=a.cmd, flags=a.flags, tick=a.tick)
        pathlib.Path(a.json).write_text(json.dumps(doc, indent=1))
    return r["rc"]


if __name__ == "__main__":
    sys.exit(main())
