"""Is the cluster's operand rate limited by MEMORY, or by gaps in the
instruction stream? On silicon, no model.

    python scripts/py/silicon_gap.py

Holds the problem fixed and varies only what changes work-in-flight: the tile
(`gm`, `gn`) and the K-chunk count (`nk`). Memory-bound would give a flat
B/cycle; a rate that climbs with in-flight work says the stream has gaps.
"""

import kohakutpu.units  # noqa: F401
import numpy as np
from kohakuaccel.device import control_read
from kohakuaccel.device.registers import CU_COUNTERS
from kohakuaccel.transport.jtag import JtagTransport
from kohakutpu.host import Card
from kohakutpu.meshes import MeshGroup
from kohakutpu.ops import matmul

BOARD = "multimesh_v7"
M, K, N = 64, 256, 128


def busy(mesh) -> int:
    total = 0
    for coord in mesh.coords("MG"):
        word = control_read(mesh.ctrl, coord, CU_COUNTERS)
        if word is not None:
            total += word & 0xFFFF_FFFF
    return total


def main() -> int:
    t = JtagTransport()
    card = Card.from_board(BOARD, transport=t, which=[0])
    mesh = card.mesh
    head = MeshGroup.open(card, [0])[0]
    rng = np.random.default_rng(11)

    a = rng.normal(0, 0.02, (M, K)).astype(np.float16)
    b = rng.normal(0, 1.00, (N, K)).astype(np.float16)
    want = a.astype(np.float32) @ b.astype(np.float32).T
    scale = np.maximum(np.abs(want), np.abs(want).max() * 1e-3)
    nbytes = (M * K + N * K) * 2

    print(f"{mesh}\n{M}x{K}x{N}, operands {nbytes:,} B, ON SILICON\n")
    print(
        f"{'gm':>3}{'gn':>4}{'nk':>4} {'busy cyc':>10} {'B/cyc':>8} "
        f"{'vs 2.61':>8} {'p50 rel':>10}"
    )

    for gm, gn, nk in (
        (16, 8, 2),
        (8, 16, 2),
        (16, 16, 2),
        (16, 16, 4),
        (32, 16, 2),
        (16, 32, 2),
        (32, 32, 2),
        (16, 16, 8),
    ):
        try:
            before = busy(mesh)
            got = matmul(head.tensor(a), head.tensor(b), gm=gm, gn=gn, nk=nk).numpy()
            cycles = (busy(mesh) - before) % (1 << 32)
        except Exception as exc:  # noqa: BLE001
            print(
                f"{gm:>3}{gn:>4}{nk:>4}  refused: {type(exc).__name__}: "
                f"{str(exc)[:48]}"
            )
            continue
        rel = np.abs(got - want) / scale
        per = nbytes / max(cycles, 1)
        print(
            f"{gm:>3}{gn:>4}{nk:>4} {cycles:>10,} {per:>8.2f} "
            f"{per / 2.61:>7.2f}x {np.percentile(rel, 50):>10.3e}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
