"""The package FETCH step on a card model: a matmul's cluster words placed in
memory and fetched by each cluster's port from there, against the same words
sent from the package.

    python scripts/py/l1/fetch_check.py --build build/v9v2c/vlt_card_v9_1n_v2

Both products must be bit-identical, and graded against numpy.
"""

import argparse
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from card import L1Card
from check import grade
from kohakutpu.hw.mxfp7 import value_fp16
from kohakutpu.ir.l1 import Program
from kohakutpu.ir.l1.kernels import matmul as MM

WORD = 32


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", required=True)
    ap.add_argument("--shape", default="256,256,256", help="M,K,N")
    ap.add_argument("--tile", default="64,64,2", help="gm,gn,nk")
    a = ap.parse_args()
    m, k, n = (int(v) for v in a.shape.split(","))
    gm, gn, nk = (int(v) for v in a.tile.split(","))
    card = L1Card(a.build)
    rng = np.random.default_rng(3)
    x = (rng.standard_normal((m, k)) * 0.5).astype(np.float16)
    w = (rng.standard_normal((n, k)) * 0.5).astype(np.float16)
    a_at = card.put(np.frombuffer(MM.pack_a(x, gm, nk), np.uint8))
    b_at = card.put(np.frombuffer(MM.pack_b(w, gn, nk), np.uint8))
    got = {}
    for how in ("send", "fetch"):
        c_at = card.alloc(m * n * 2)
        prog = Program(card.machine)
        tiles, fetched = [], 0
        for u, ops, drained in MM.cluster_ops(
            prog, a_at, b_at, c_at, m, n, k, gm, gn, nk
        ):
            if how == "send":
                prog.send(u, *ops)
            else:
                words = [wd for op in ops for wd in op.flits()]
                raw = b"".join(wd.to_bytes(WORD, "little") for wd in words)
                prog.fetch(u, card.put(np.frombuffer(raw, np.uint8)), len(words))
                fetched += len(words)
            tiles += drained
        prog.barrier()
        res = card.run(prog)
        kinds = sorted({s.op.name for s in res["pkg"].steps})
        where = sorted(tiles, key=lambda t: t[3])
        got[how] = MM.unpack(card.get, where, m, n, gm, gn)
        print(
            f"{how}: {res['cycles']} node cycles  steps {kinds}  "
            f"words fetched from memory {fetched}",
            flush=True,
        )
    same = np.array_equal(got["send"], got["fetch"])
    print(f"fetch == send bit for bit: {same}", flush=True)
    model = value_fp16(x) @ value_fp16(w).T
    ok = grade("fetch", got["fetch"], model, x.astype(np.float64) @ w.T.astype(float))
    card.close()
    good = same and ok
    print("PASS" if good else "FAIL", flush=True)
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
