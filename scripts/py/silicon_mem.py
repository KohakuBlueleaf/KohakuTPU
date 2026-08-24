"""On-silicon memory measurement. Nothing here is modelled.

    python scripts/py/silicon_mem.py

Every figure is read from the card: cluster `busy_cycles` from `CU_COUNTERS`,
and error percentiles against a float64 reference. The bandwidth is the SLOPE of
busy cycles against operand bytes across shapes, not a single point -- a single
point carries the fixed dispatch cost and reads low.
"""

import kohakutpu.units  # noqa: F401  (registers MG and VC)
import numpy as np
from kohakuaccel.device import control_read
from kohakuaccel.device.registers import CU_COUNTERS
from kohakuaccel.transport.jtag import JtagTransport
from kohakutpu.host import Card
from kohakutpu.meshes import MeshGroup
from kohakutpu.ops import matmul

BOARD = "multimesh_v7"


def busy(mesh) -> int:
    """Total busy cycles over every cluster on this mesh."""
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

    print(f"{mesh}")
    print("ON SILICON -- mesh 0, clocks as programmed. No model anywhere.\n")
    print(
        f"{'M':>4}{'K':>6}{'N':>5} {'operand B':>10} {'busy cyc':>10} "
        f"{'B/cyc':>8} {'p50 rel':>10} {'p99 rel':>10}"
    )

    rows = []
    for k in (128, 256, 512):
        m, n = 64, 128
        a = rng.normal(0, 0.02, (m, k)).astype(np.float16)
        b = rng.normal(0, 1.00, (n, k)).astype(np.float16)
        want = a.astype(np.float32) @ b.astype(np.float32).T

        before = busy(mesh)
        got = matmul(head.tensor(a), head.tensor(b), gm=8, gn=8, nk=2).numpy()
        cycles = (busy(mesh) - before) % (1 << 32)

        nbytes = (m * k + n * k) * 2
        scale = np.maximum(np.abs(want), np.abs(want).max() * 1e-3)
        rel = np.abs(got - want) / scale
        rows.append((nbytes, cycles))
        print(
            f"{m:>4}{k:>6}{n:>5} {nbytes:>10,} {cycles:>10,} "
            f"{nbytes / max(cycles, 1):>8.2f} "
            f"{np.percentile(rel, 50):>10.3e} {np.percentile(rel, 99):>10.3e}"
        )

    if len(rows) > 1:
        d_bytes = rows[-1][0] - rows[0][0]
        d_cycles = rows[-1][1] - rows[0][1]
        per = d_bytes / max(d_cycles, 1)
        print(f"\nslope: +{d_bytes:,} B / +{d_cycles:,} cyc = {per:.2f} B/cycle")
        print(
            f"  = {per * 100e6 / 1e9:.2f} GB/s at a 100 MHz clock, "
            f"{per * 300e6 / 1e9:.2f} at 300 MHz"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
