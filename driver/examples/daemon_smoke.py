"""On-card daemon smoke: register paths and clock policy only.

Deliberately touches nothing DRAM-shaped: this board's host write path
has its own byte semantics, proven elsewhere. What a daemon smoke must
prove is the held session (register reads answer), the governor
(scoped boost, idle drop) and the lease surface -- against the live
card, through the daemon, with the raw numbers printed.

    python -m kohakuaccel.daemon --board multimesh_v65 --idle-seconds 5 &
    python driver/examples/daemon_smoke.py
"""

import argparse
import time

from kohakuaccel.daemon.client import DaemonClient
from kohakuaccel.device.registers import A_CAPS
from kohakutpu.clock.card import load_board

#: The caps word self-describes: flit width low, 1..8 units above it.
#: 288 is what every build of this mesh reports (the NoC flit).
FLIT_WIDTH = 288


def _is_caps(word: int) -> bool:
    return (word & 0xFFFF) == FLIT_WIDTH and 0 < ((word >> 16) & 0xFF) <= 8


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=47155)
    ap.add_argument("--board", default="multimesh_v65")
    args = ap.parse_args()

    board = load_board(args.board)
    ctrl = int(board["ctrl_base"], 16)
    stride = int(board["ctrl_stride"], 16)
    c = DaemonClient(port=args.port)
    print(f"hello: {c.hello}")

    print("| mesh | caps word | caps-shaped |")
    print("|---|---|---|")
    for i in range(board["meshes"]):
        word = c.call("read32", addr=ctrl + i * stride + A_CAPS)
        print(f"| {i} | {word:#010x} | {_is_caps(word)} |")

    def show(tag):
        clocks = c.clocks()
        print(
            f"| {tag} | "
            + " | ".join(
                str({k: round(v, 1) for k, v in clocks[m].items()})
                for m in sorted(clocks)
            )
            + " |"
        )

    print("| state | mesh0 | mesh1 | mesh2 | mesh3 |")
    print("|---|---|---|---|---|")
    show("startup")

    token = c.run_begin([0], "mid")
    show("mesh0 mid")
    c.run_end(token)

    idle = c.status()["governor"]["idle_seconds"]
    time.sleep(idle + 2.5)
    show("after idle")

    lease = c.claim(mesh=0, base=0x1800_0000, size=1 << 24)
    try:
        c.claim(mesh=0, base=0x1880_0000, size=1 << 24)
        print("| lease overlap | NOT refused |")
    except Exception as exc:  # noqa: BLE001
        print(f"| lease overlap | refused: {type(exc).__name__} |")
    c.release(lease)

    t0 = time.perf_counter()
    n = 50
    for _ in range(n):
        c.call("read32", addr=ctrl + A_CAPS)
    dt = time.perf_counter() - t0
    print(f"| read32 x{n} | {dt:.2f} s | {n / dt:.1f} ops/s |")


if __name__ == "__main__":
    main()
