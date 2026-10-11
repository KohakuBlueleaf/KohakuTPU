"""Run one `.ktpu` image on the V2 core alone (`vec_replay_tb`, as
`kohakutpu.simulation.verilator.replay`): seconds a run, for scheduling work
before a card run. The replay memory is 128 KB and answers one word every
memwait + 1 cycles with no round-trip latency, so a card run still gates
memory effects.

    python -m kohakutpu.application.tools.core softmax softmax_pipe passes=4 \
        x=in:16384 y=out:16384 [--memwait 1]

Each image parameter is an integer or a buffer: `in:N` (N seeded-random fp16)
or `out:N` (N fp16, zeroed). Prints busy cycles, lane use and every engine's
busy and stall counters; `--out FILE` appends the row as JSON.
"""

import argparse
import hashlib
import json
import pathlib
import re
import subprocess
import sys

import numpy as np
from kohakutpu.compiler.emit import image as vector
from kohakutpu.compiler.encode import vector as A
from kohakutpu.language import kernels as ktpu
from kohakutpu.simulation.verilator import replay as bench

MASK = (1 << 256) - 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "kernel", help="a kernel name under language/kernels, or a .ktpu path"
    )
    ap.add_argument("image")
    ap.add_argument("params", nargs="*", help="NAME=INT | NAME=in:N | NAME=out:N")
    ap.add_argument("--memwait", type=int, default=1)
    ap.add_argument("--seed", type=int, default=2)
    ap.add_argument("--scale", type=float, default=2.0)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    path = pathlib.Path(a.kernel)
    m = ktpu.load(path if path.suffix == ".ktpu" else ktpu.kernel(a.kernel))
    given = dict(p.split("=", 1) for p in a.params)
    rng = np.random.default_rng(a.seed)
    mem = bytearray(bench.WORDS * 32)
    at, args = 0x400, []
    for p in m.images[a.image].params:
        v = given[p]
        if ":" not in v:
            args.append(int(v, 0))
            continue
        kind, n = v.split(":")
        x = (rng.standard_normal(int(n)) * a.scale).astype(np.float16)
        raw = x.tobytes() if kind == "in" else bytes(len(x.tobytes()))
        mem[at : at + len(raw)] = raw
        args.append(at)
        at += -(-len(raw) // 256) * 256 + 256
    if at > len(mem):
        raise SystemExit(f"{at} bytes pass the 128 KB replay memory")
    words, descs = vector.image(m, a.image, tuple(args))
    flits = [A.desc_flit(ad, f, v) for (ad, f), v in sorted(descs.items())]
    flits += [A.imem_flit(i, w) for i, w in enumerate(words)]
    flits.append(A.run_flit(0))
    # The model truncates long plusarg paths: the image and a short digest.
    digest = hashlib.sha1(repr(sorted(given.items())).encode()).hexdigest()[:8]
    tag = f"{a.image}_{digest}"
    run_dir = bench.BUILD / "run" / f"ktpu_{tag}_mw{a.memwait}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "prog.hex").write_text("".join(f"{f & MASK:064x}\n" for f in flits))
    lines = [
        int.from_bytes(mem[32 * i : 32 * i + 32], "little") for i in range(bench.WORDS)
    ]
    (run_dir / "mem.hex").write_text("".join(f"{v:064x}\n" for v in lines))
    if not bench.vsim().exists():
        bench.build_model()
    plus = [
        f"+{k}={(run_dir / f'{k}.hex').resolve().as_posix()}"
        for k in ("prog", "mem", "out")
    ]
    done = subprocess.run(
        [str(bench.vsim()), *plus, f"+memwait={a.memwait}"],
        cwd=bench.vsim().parent,
        capture_output=True,
        text=True,
        check=False,
    )
    log = done.stdout + done.stderr
    (run_dir / "log.txt").write_text(log)
    rep = re.search(r"@@@ REPLAY payloads (\d+) completions (\d+) faults (\d+)", log)
    vres = re.search(r"VRES \S+ (.*)", log)
    if not (rep and vres):
        print(log[-3000:])
        raise SystemExit("no replay result")
    kv = vres.group(1).split()
    v = {kv[i]: int(kv[i + 1]) for i in range(0, len(kv) - 1, 2)}
    eng = {}
    for e in re.finditer(r"(V2\w+) vec_replay_tb\S* (.*)", log):
        kv2 = e[2].split()
        eng[e[1]] = {kv2[i]: int(kv2[i + 1]) for i in range(0, len(kv2) - 1, 2)}
    lane = v["lanes"] / (16 * v["busy"]) if v["busy"] else 0.0
    row = {
        "kernel": a.kernel,
        "image": a.image,
        "params": given,
        "memwait": a.memwait,
        "imem": len(words),
        "faults": int(rep[3]),
        "busy": v["busy"],
        "lanes": v["lanes"],
        "lane": lane,
        "res": v,
        "eng": eng,
    }
    print(
        f"{a.image} {given} mw{a.memwait}: busy {v['busy']} lane {lane:.1%} "
        f"imem {len(words)} faults {rep[3]}  ({run_dir})"
    )
    for k, d in eng.items():
        print(f"  {k} {d}")
    if a.out:
        with pathlib.Path(a.out).open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
