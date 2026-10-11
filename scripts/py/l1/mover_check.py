"""The node mover's transforms and configuration queue on a card model, data
checked against host references.

    python scripts/py/l1/mover_check.py --build build/v9v2b/vlt_card_v9_1n_v2

* quantise: FP16 entries -> MXFP7 through the slot, decoded and compared with
  `kohakutpu.hw.mxfp7.quantise_fp16` element by element;
* GT4: groups of four words transposed as 4 x 4 arrays of 64-bit granules,
  compared with a numpy transpose;
* queue: the same copies as one posted step (every move's registers queued
  behind the previous GO) and as one step a move, data checked both ways.
"""

import argparse
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from card import L1Card
from kohakutpu.hw import mxfp7
from kohakutpu.hw.tensor import from_mxfp7_entries
from kohakutpu.ir.l1 import Program
from kohakutpu.ir.l1.mover import Copy, Quantise, Transpose4

WORD = 32


def awkward(entries: int, seed: int) -> np.ndarray:
    """Wide exponents, subnormals, zeros and the extremes; (entries*4, 32)."""
    rng = np.random.default_rng(seed)
    x = rng.standard_normal((entries * 4, 32)) * (
        10.0 ** rng.integers(-6, 5, (entries * 4, 1))
    )
    x[0] = 0.0
    x[1, :6] = [0.0, 65504.0, -65504.0, 6.1e-5, -6.0e-8, 1.0]
    x[2] = 6.0e-8
    x[3, 0] = 65504.0
    return x.astype(np.float16)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", required=True)
    ap.add_argument("--entries", type=int, default=256)
    ap.add_argument("--groups", type=int, default=256)
    ap.add_argument("--moves", type=int, default=8)
    ap.add_argument("--move-kb", type=int, default=4)
    a = ap.parse_args()
    card = L1Card(a.build)
    rng = np.random.default_rng(7)
    bad = 0

    def run(build) -> int:
        p = Program(card.machine)
        build(p)
        return card.run(p)["cycles"]

    # ---- quantise ----
    x = awkward(a.entries, 1)
    src = card.put(x)
    dst = card.alloc(a.entries * 4 * WORD)
    c = run(lambda p: p.move(Quantise(src, dst, a.entries)))
    q, es, m8 = from_mxfp7_entries(card.get(dst, a.entries * 4 * WORD))
    qw, esw, m8w = mxfp7.quantise_fp16(x.astype(np.float32))
    qw = np.asarray(qw).reshape(a.entries, 4, 32)
    esw = np.asarray(esw).reshape(a.entries, 4)
    m8w = np.asarray(m8w).reshape(a.entries, 4)
    n_q = int((q != qw).sum())
    n_s = int((es != esw).sum() + (m8 != m8w).sum())
    bad += n_q + n_s
    print(
        f"quantise {a.entries} entries: {c} node cycles  "
        f"significands {q.size - n_q}/{q.size}  scales {2 * es.size - n_s}/{2 * es.size}",
        flush=True,
    )

    # ---- GT4 ----
    g = rng.integers(0, 256, (a.groups, 4, 4, 8), dtype=np.uint8)
    src = card.put(g)
    dst = card.alloc(g.nbytes)
    c = run(lambda p: p.move(Transpose4(src, dst, a.groups)))
    got = np.frombuffer(card.get(dst, g.nbytes), np.uint8).reshape(g.shape)
    n_g = int((got != g.transpose(0, 2, 1, 3)).sum())
    bad += n_g
    print(
        f"GT4 {a.groups} groups: {c} node cycles  bytes {g.nbytes - n_g}/{g.nbytes}",
        flush=True,
    )

    # ---- queue: posted back to back vs one step a move ----
    nb = a.move_kb << 10
    data = rng.integers(0, 256, a.moves * nb, dtype=np.uint8)
    src = card.put(data)
    for label, posted in (("one posted step", True), ("a step a move", False)):
        dst = card.alloc(data.nbytes)
        ops = [Copy(src + k * nb, dst + k * nb, nb) for k in range(a.moves)]

        def build(p, ops=ops, posted=posted):
            if posted:
                p.post(*ops)
                p.barrier()
            else:
                for op in ops:
                    p.move(op)

        c = run(build)
        got = np.frombuffer(card.get(dst, data.nbytes), np.uint8)
        n_c = int((got != data).sum())
        bad += n_c
        print(
            f"{a.moves} copies of {a.move_kb} KB, {label}: {c} node cycles  "
            f"bytes {data.nbytes - n_c}/{data.nbytes}",
            flush=True,
        )

    card.close()
    print("PASS" if bad == 0 else f"FAIL {bad} mismatches", flush=True)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
