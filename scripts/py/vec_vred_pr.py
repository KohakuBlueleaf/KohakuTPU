"""VRED ANY/ALL on the vector core alone: each reduces ITS OWN predicate, after
the compare that writes it has retired.

    python scripts/py/vec_vred_pr.py [--bench vec_replay] [--build-root build/sw]

Runs on the vec_replay bench binary (scripts/py/vlt.py vec_replay --keep, the
replay tb of scripts/py/vec_replay.py). P1 is set all true and P2 all false by
VCMPEQ of a 1.0 broadcast against 1.0 / 2.0. Each VRED names a predicate other
than the one the compare right before it wrote, or reads one that compare is
still writing; each result is broadcast, stored, drained, and compared with
kohakutpu.model.VectorUnit and the expected value.
"""

import argparse
import pathlib
import re
import subprocess
import sys

import numpy as np
from kohakuaccel.sim.machine import Memory
from kohakutpu.hw import vector as V
from kohakutpu.hw.veckernels import imem_flits
from kohakutpu.isa.relayout import _dims
from kohakutpu.model import VectorUnit

ROOT = pathlib.Path(__file__).resolve().parents[2]
DST, WORDS = 0x8000, 4096
PAYLOAD = (1 << 256) - 1
AD_ST, AD_DR = 2, 1
ONE, ZERO = 0x3C00, 0x0000


def wsl(p: pathlib.Path) -> str:
    s = p.resolve().as_posix()
    m = re.match(r"^([A-Za-z]):/(.*)$", s)
    return f"/mnt/{m.group(1).lower()}/{m.group(2)}" if m else s


def cmp(pr: int, s: int) -> int:
    return V.alu("VCMPEQ", vd=0, va=0, vb=s, sb=V.SRC_S, pr=pr)


#: (what, the words that set S[dst], dst, expected fp16)
CASES = [
    (
        "ANY(P1) after a compare into P2",
        [cmp(1, 1), cmp(2, 2), V.vred(5, 0, "ANY", pr=1)],
        5,
        ONE,
    ),
    (
        "ALL(P1) after a compare into P2",
        [cmp(1, 1), cmp(2, 2), V.vred(6, 0, "ALL", pr=1)],
        6,
        ONE,
    ),
    (
        "ANY(P2) after a compare into P1",
        [cmp(2, 2), cmp(1, 1), V.vred(7, 0, "ANY", pr=2)],
        7,
        ZERO,
    ),
    (
        "ALL(P2) after a compare into P1",
        [cmp(2, 2), cmp(1, 1), V.vred(9, 0, "ALL", pr=2)],
        9,
        ZERO,
    ),
    # P1 false -> true by the compare right before the VRED (in flight at issue).
    (
        "ANY(P1) right after setting P1",
        [cmp(1, 2), cmp(2, 2), cmp(1, 1), V.vred(10, 0, "ANY", pr=1)],
        10,
        ONE,
    ),
    # P2 true -> false by the compare right before the VRED.
    (
        "ANY(P2) right after clearing P2",
        [cmp(2, 1), cmp(2, 2), V.vred(11, 0, "ANY", pr=2)],
        11,
        ZERO,
    ),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", default="vec_replay")
    ap.add_argument("--build-root", default="build/sw")
    a = ap.parse_args()

    img = [V.vseti(0), 16, V.vseti(1), V.e8m15(1.0), V.vseti(2), V.e8m15(2.0)]
    img += [V.vsetvl(0), V.vsetmode(V.FLAT), V.vbcast(0, 1)]
    for _, words, _, _ in CASES:
        img += words
    for n, (_, _, s, _) in enumerate(CASES):
        img += [V.vbcast(1, s), V.vst(1, AD_ST, n)]
    img += [V.vdrain(AD_DR, 0), V.vhalt()]
    flits = imem_flits(img)
    flits += [V.desc_flit(AD_ST, 0, 0), *_dims(AD_ST, [(1, 1)])]
    flits += [
        V.desc_flit(AD_DR, 0, DST),
        *_dims(AD_DR, [(V.WORD_BYTES, len(CASES))]),
    ]
    flits += [V.run_flit(0)]
    payloads = [f & PAYLOAD for f in flits]

    mem = Memory(WORDS * 32)
    run = ROOT / a.build_root / "vec_vred_pr_run"
    run.mkdir(parents=True, exist_ok=True)
    (run / "prog.hex").write_text("".join(f"{p:064x}\n" for p in payloads))
    (run / "mem.hex").write_text("".join(f"{0:064x}\n" for _ in range(WORDS)))
    args = (
        f"+prog={wsl(run / 'prog.hex')} +mem={wsl(run / 'mem.hex')} "
        f"+out={wsl(run / 'out.hex')}"
    )
    work = ROOT / a.build_root / f"vlt_{a.bench}"
    if not (work / "obj_dir" / "vsim").exists():
        raise SystemExit(
            f"no bench at {work}; python scripts/py/vlt.py {a.bench} --keep "
            f"--build-root {a.build_root}"
        )
    done = subprocess.run(
        [
            "wsl",
            "-d",
            "Ubuntu-24.04",
            "--",
            "bash",
            "-lc",
            f"cd {wsl(work)} && ./obj_dir/vsim {args}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    out = done.stdout + done.stderr
    print("\n".join(ln for ln in out.splitlines() if "@@@ REPLAY" in ln))
    lines = [
        int(ln, 16)
        for ln in (run / "out.hex").read_text().split()
        if ln and ln[0] not in "@/"
    ]
    rtl = b"".join(v.to_bytes(32, "little") for v in lines)

    unit = VectorUnit(mem_base=0)
    for p in payloads:
        unit.execute(p, mem)

    bad = 0
    for n, (what, _, _, want) in enumerate(CASES):
        at = DST + 32 * n
        r = int(np.frombuffer(rtl[at : at + 2], np.uint16)[0])
        m = int(np.frombuffer(mem.read(at, 2), np.uint16)[0])
        ok = r == want and m == want
        bad += not ok
        print(
            f"  {what:34s} want {want:04x}  RTL {r:04x}  model {m:04x}  "
            f"{'ok' if ok else 'WRONG'}"
        )
    print("PASS" if not bad else f"FAIL ({bad} of {len(CASES)})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
