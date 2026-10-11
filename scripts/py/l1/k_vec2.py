"""Card run of the V2 vector kernels (`scripts/py/vec2/kernels.py`) on a card
model built with V2 vector cores (bench `card_v9_1n_v2`, `-d VEC_CORE_V2`).

    python scripts/py/l1/k_vec2.py --build build/v9v2/vlt_card_v9_1n_v2 \
        --case softmax:rows=128 --case attn_om:rs=0,blocks=8 [--cores 2]

Each listed core runs its own instance on its own data, all at once. Each case
runs twice; the second (image and descriptors resident) is reported. Lane use is
lane beats over 16 lanes x cores x node cycles.
"""

import argparse
import pathlib
import sys

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "vec2"))
import kernels
from card import L1Card
from kohakutpu.ir.l1 import Program
from kohakutpu.ir.l1.vector2 import Kernel


def parse(spec: str) -> tuple[str, dict]:
    name, _, rest = spec.partition(":")
    opts = {}
    for kv in filter(None, rest.split(",")):
        k, v = kv.split("=", 1)
        for cast in (int, float, str):
            try:
                opts[k] = cast(v)
                break
            except ValueError:
                continue
    return name, opts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", required=True)
    ap.add_argument("--case", action="append", required=True, help="kernel:k=v,...")
    ap.add_argument("--cores", type=int, default=2)
    a = ap.parse_args()
    for spec in a.case:
        name, opts = parse(spec)
        card = L1Card(a.build)
        cores = Program(card.machine).units("VC")[: a.cores]
        outs, prog = [], Program(card.machine)
        for core in cores:
            build, arrays, ref = getattr(kernels, name)(**opts)
            addr = {k: card.put(np.ascontiguousarray(v)) for k, v in arrays.items()}
            want = ref()
            addr["out"] = card.alloc(want.size * 2)
            kern = build(addr)
            prog.send(core, Kernel(kern))
            outs.append((core, addr["out"], want, len(kern.words)))
        c_cold = card.run(prog)["cycles"]
        got = card.run(prog)
        c_warm = got["cycles"]
        errs = []
        for core, at, want, _ in outs:
            y = np.frombuffer(card.get(at, want.size * 2), np.float16)
            y = y.astype(np.float64).reshape(want.shape)
            errs.append(float(np.abs(y - want).max() / max(np.abs(want).max(), 1e-30)))
        stats = card.close()
        busy = [c["vres"].get("busy", 0) // 2 for _, c in sorted(stats.items())]
        beats = [c["vres"].get("lanes", 0) // 2 for _, c in sorted(stats.items())]
        lane = sum(beats) / (16 * len(cores) * c_warm)
        print(
            f"{name} {opts} x{len(cores)} cores: {c_warm} cycles (cold {c_cold})  "
            f"lane {lane:.1%}  core busy {busy} lane-beats {beats}  "
            f"err {[f'{e:.2e}' for e in errs]}  imem {outs[0][3]}  "
            f"phases {got['phases']}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
