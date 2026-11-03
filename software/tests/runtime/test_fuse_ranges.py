"""A contraction's heads over N and its row batches over M as one product
(`KTPU_MM_FUSE_RANGES`, `planning.lower.fuse_ranges`). GPT's fused QKV
projection, read as [8, 28, 3, 2, 64] by three heads' slices, is a vector
plan without it; with it, one [224, 128] x [384, 128] product. Both plans,
and both of attention's values merged back by head, make the same bytes; the
QKV's error against f32 and the MXFP7 simulation is reported. Plans are kept
by kernel for the process, so each knob runs in a process of its own."""

import json
import os
import pathlib
import subprocess
import sys

import numpy as np
from tinygrad.tensor import Tensor

from kohakutpu.application import ktpugrad
from kohakutpu.application.checks import accuracy as A
from kohakutpu.application.ktpugrad import uops as U
from kohakutpu.application.ktpugrad.planning import lower as L
from kohakutpu.application.runtime import device as card

RNG = np.random.default_rng(13)
X = RNG.standard_normal((8, 28, 128)).astype(np.float16)
W3 = (RNG.standard_normal((384, 128)) / 8).astype(np.float16)
B3 = RNG.standard_normal(384).astype(np.float16)
P = (RNG.random((8, 2, 28, 28)) / 28).astype(np.float16)
V = RNG.standard_normal((8, 2, 28, 64)).astype(np.float16)


def products(library: str, out: pathlib.Path) -> None:
    """The heads on a fresh double device: their values into `out` (.npy)
    and each product planned, ``[bt, m, n, k]``, into `out` (.json)."""
    seen = []
    real = L.plan

    def planned(ast):
        p = real(ast)
        if p.kind == "matmul":
            m, n, k = p.mnk
            bt = int(np.prod([U.extent(r) for r in p.batch])) if p.batch else 1
            kk = int(np.prod([U.extent(r) for r in k]))
            seen.append([bt, U.extent(m), U.extent(n), kk, len(p.splits)])
        return p

    L.plan = planned
    ktpugrad.install()
    dev = card.Device(kind="double", board="multimesh_v9t6", nodes=1, library=library)
    with card.attached(dev):
        x = Tensor(X, device="KTPU")
        w, b = Tensor(W3, device="KTPU"), Tensor(B3, device="KTPU")
        qkv = x.matmul(w.T).add(b).reshape(8, 28, 3, 2, 64)
        y = qkv[:, :, 0].mul(qkv[:, :, 1]).add(qkv[:, :, 2]).transpose(1, 2)
        # Attention's values by head, the heads back in the width (a split).
        p, v = Tensor(P, device="KTPU"), Tensor(V, device="KTPU")
        z = p.matmul(v).transpose(1, 2).reshape(8, 28, 128)
        np.savez(
            out.with_suffix(".npz"), y=y.contiguous().numpy(), z=z.contiguous().numpy()
        )
    dev.close()
    out.with_suffix(".json").write_text(json.dumps(seen), "utf-8")


def run(knob: str, tmp_path: pathlib.Path) -> tuple:
    out = tmp_path / f"fuse{knob}"
    env = dict(os.environ, KTPU_MM_FUSE_RANGES=knob)
    cmd = [sys.executable, __file__, str(tmp_path / "lib"), str(out)]
    subprocess.run(cmd, env=env, check=True)
    seen = [tuple(p) for p in json.loads(out.with_suffix(".json").read_text("utf-8"))]
    got = np.load(out.with_suffix(".npz"))
    return got["y"], got["z"], seen


def heads_of(x, w) -> np.ndarray:
    """The queries' product with the keys plus the values, as the test reads them."""
    qkv = (x @ w.T + B3.astype(np.float32)).reshape(8, 28, 3, 2, 64)
    return (qkv[:, :, 0] * qkv[:, :, 1] + qkv[:, :, 2]).transpose(0, 2, 1, 3)


def test_heads_and_row_batches_are_one_product(tmp_path, capsys):
    y0, z0, planned = run("0", tmp_path)
    assert (1, 224, 384, 128, 0) not in planned, planned
    y1, z1, planned = run("1", tmp_path)
    assert (1, 224, 384, 128, 0) in planned, planned
    assert any(p[4] for p in planned), planned
    # The head merge moves the same products: the same bytes.
    assert z1.tobytes() == z0.tobytes()
    # Either plan of the QKV sums each output's K blocks in one order: the same bytes.
    assert y1.tobytes() == y0.tobytes()
    # Their error against f32 and the MXFP7 simulation, reported, no cutoff.
    f32 = heads_of(X.astype(np.float32), W3.astype(np.float32))
    mx = heads_of(A.operand(X, -1), A.operand(W3, -1))
    report = A.output_report(y1, f32, mx)
    del report["worst"]
    with capsys.disabled():
        print("\nQKV:", json.dumps(report))


if __name__ == "__main__":
    products(sys.argv[1], pathlib.Path(sys.argv[2]))
