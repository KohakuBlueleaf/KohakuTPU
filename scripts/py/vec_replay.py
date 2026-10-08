"""Replay one compiler-emitted relayout on the vector core alone, against the model.

    python scripts/py/vec_replay.py [--shape 16,16 --gm 4 --gn 4] [--build-root build/sw]
                                    [--before tile --after flat] [--rebuild]

Builds the relayout program the runtime would send for `before -> after` over
one tensor (the same `RL.for_conversion` + `RL.build(...).program` call
`kohakutpu.rt.Holder.reorder` makes), lays the source and the lane-groups mask
into a memory image, runs `tests/vector/vec_replay_tb.v` (vec_cu, Verilator)
on it, and compares the destination ELEMENT BY ELEMENT with
  - kohakutpu.model.VectorUnit running the same payloads, and
  - the destination order packed directly from the array (independent of both).
Prints which elements differ and how, so a divergence names its pattern.
"""

import argparse
import pathlib
import re
import subprocess
import sys

import numpy as np
from kohakuaccel.sim.machine import Memory
from kohakutpu.isa import relayout as RL
from kohakutpu.model import VectorUnit

from kohakutpu import layout as LO

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC, DST, MASK = 0x1000, 0x8000, 0x10000
WORDS = 4096
PAYLOAD = (1 << 256) - 1


def wsl(p: pathlib.Path) -> str:
    s = p.resolve().as_posix()
    m = re.match(r"^([A-Za-z]):/(.*)$", s)
    return f"/mnt/{m.group(1).lower()}/{m.group(2)}" if m else s


def layout(name: str, shape, gm: int, gn: int):
    if name == "tile":
        return LO.Tile((shape[0] // (4 * gm), shape[1] // (4 * gn)), gm, gn)
    if name == "flat":
        return LO.Flat()
    if name == "entry":
        return LO.Entry(gm, gn)
    raise SystemExit(f"no layout {name!r}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shape", default="16,16")
    ap.add_argument("--gm", type=int, default=4)
    ap.add_argument("--gn", type=int, default=4)
    ap.add_argument("--before", default="tile")
    ap.add_argument("--after", default="flat")
    ap.add_argument("--build-root", default="build/sw")
    ap.add_argument("--bench", default="vec_replay")
    ap.add_argument("--rebuild", action="store_true")
    a = ap.parse_args()
    shape = tuple(int(x) for x in a.shape.split(","))
    before, after = layout(a.before, shape, a.gm, a.gn), layout(
        a.after, shape, a.gm, a.gn
    )

    rng = np.random.default_rng(21)
    x = (rng.standard_normal(shape) * 2).astype(np.float16)
    made = RL.for_conversion(before, after, shape)
    if made is None:
        raise SystemExit(f"no walk for {before.key} -> {after.key} at {shape}")
    plan, _, _, count = made
    flits = RL.build(plan).program([(SRC, DST)], MASK if plan.transpose else 0)
    payloads = [f & PAYLOAD for f in flits]
    mem = Memory(WORDS * 32)
    mem.write(SRC, before.pack(x))
    if plan.transpose:
        mem.write(MASK, LO.Flat().pack(RL.lane_groups()))
    nbytes = after.nbytes(shape)

    work = ROOT / a.build_root / f"vlt_{a.bench}"
    run = ROOT / a.build_root / "vec_replay_run"
    run.mkdir(parents=True, exist_ok=True)
    (run / "prog.hex").write_text("".join(f"{p:064x}\n" for p in payloads))
    lines = [int.from_bytes(mem.read(32 * i, 32), "little") for i in range(WORDS)]
    (run / "mem.hex").write_text("".join(f"{v:064x}\n" for v in lines))
    args = f"+prog={wsl(run / 'prog.hex')} +mem={wsl(run / 'mem.hex')} +out={wsl(run / 'out.hex')}"
    if a.rebuild or not (work / "obj_dir" / "vsim").exists():
        done = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/py/vlt.py"),
                a.bench,
                "--keep",
                "--build-root",
                a.build_root,
                "--run-args",
                args,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        out = done.stdout + done.stderr
    else:
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
    print("\n".join(ln for ln in out.splitlines() if "@@@" in ln or "Error" in ln))

    got_lines = []
    for ln in (run / "out.hex").read_text().splitlines():
        ln = ln.strip()
        if ln and not ln.startswith(("@", "//")):
            got_lines.append(int(ln, 16))
    rtl = b"".join(v.to_bytes(32, "little") for v in got_lines)[DST : DST + nbytes]

    model = Memory(WORDS * 32)
    model.write(0, mem.read(0, WORDS * 32))
    unit = VectorUnit(mem_base=0)
    for p in payloads:
        unit.execute(p, model)
    ref = model.read(DST, nbytes)
    want = after.pack(x)

    def elems(b):
        return after.unpack(b, shape)

    rv, mv, wv = elems(rtl), elems(ref), elems(want)
    print(
        f"  program: {len(payloads)} payloads ({count} buffer), transpose={plan.transpose}, "
        f"{before.key} -> {after.key} at {shape}"
    )
    print(f"  model vs packer:  {int(np.sum(mv != wv))} of {wv.size} elements differ")
    print(f"  RTL   vs packer:  {int(np.sum(rv != wv))} of {wv.size} elements differ")
    print(f"  RTL   vs model:   {int(np.sum(rv != mv))} of {wv.size} elements differ")
    bad = np.argwhere(rv != wv)
    if len(bad):
        flat_w = np.frombuffer(want, np.uint16)
        flat_r = np.frombuffer(rtl, np.uint16)
        wrong = np.flatnonzero(flat_w != flat_r)
        print(f"  first wrong (row, col): {bad[:12].tolist()}")
        print(
            f"  wrong byte-image elements mod 16 (lane within a 32-B word): "
            f"{np.bincount(wrong % 16, minlength=16).tolist()}"
        )
        print(
            f"  wrong elements by 32-B word, first 16 words: "
            f"{[int(np.sum((wrong // 16) == k)) for k in range(16)]}"
        )
        where = {}
        for k in wrong[:64]:
            v = flat_r[k]
            hit = np.flatnonzero(flat_w == v)
            where[int(k)] = [int(h) for h in hit[:3]]
        print(
            f"  where each wrong value belongs (element -> right places): "
            f"{dict(list(where.items())[:12])}"
        )
    rtl_p = {
        int(m.group(1)): int(m.group(2), 16)
        for m in re.finditer(r"@@@ PREG (\d) ([0-9a-f]+)", out)
    }
    for k in range(4):
        mp = sum(int(b) << i for i, b in enumerate(unit.preg[k]))
        if k in rtl_p:
            print(
                f"  P{k}: RTL {rtl_p[k]:032x}  model {mp:032x}  "
                f"{'same' if rtl_p[k] == mp else 'DIFFER'}"
            )
    ok = not np.any(rv != wv) and not np.any(mv != wv)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
