"""Drive the simulated multimesh_v8t card through the driver's own seam.

    python scripts/py/v8t_card_run.py [--build build/vlt_v8t_card]

Four things, each a fact about the image rather than about the model:
  1. every node's agent answers A_CAPS through its control window (station ->
     32->64 converter -> orchestrator);
  2. a block written through node 0's memory window reads back through nodes
     1..3 (station -> node -> Xache -> the flat 16 GB), and the DRAM behind the
     Xache holds it (write-through, checked at the backdoor);
  3. node 0's mover copies 64 words laid out across all four channels (16 KB
     stride) to another such region, and node 2 reads the copy back;
  4. the station's DECERR counter is still zero.
"""

import argparse
import pathlib
import sys
import time

from kohakuaccel.device import mover, rv64load
from kohakuaccel.device.registers import A_CAPS
from kohakuaccel.transport.verilator import VerilatorTransport
from kohakutpu.clock.card import load_board
from kohakutpu.host import board_map

ILV_LG, HOME_LSB, NHOME_LG = 14, 32, 2
ROOT = pathlib.Path(__file__).resolve().parents[2]


def rotate(addr: int) -> tuple[int, int]:
    """(home, in-channel byte address) of a flat address, the Xache's way:
    pairs (i, i+2) for i = ILV_LG .. HOME_LSB-1, applied in order."""
    a = addr
    for i in range(ILV_LG, HOME_LSB):
        j = i + NHOME_LG
        bi, bj = (a >> i) & 1, (a >> j) & 1
        a &= ~((1 << i) | (1 << j))
        a |= (bi << j) | (bj << i)
    return (a >> HOME_LSB) & 3, a & ((1 << HOME_LSB) - 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default=None)
    ap.add_argument("--native", action="store_true")
    ap.add_argument("--settle", type=int, default=40000)
    a = ap.parse_args()

    board = load_board("multimesh_v8t")
    m = board_map(board)
    ctrl, mem = m["ctrl"], m["mem"]
    fails = 0

    t0 = time.monotonic()
    t = VerilatorTransport(build_dir=a.build, wsl=not a.native, settle=a.settle)
    print(f"model up in {time.monotonic() - t0:.1f}s: {t.ready}")

    # 1 ---------------------------------------------------------------- caps
    for i in range(4):
        caps = t.read64(ctrl[i] + A_CAPS)
        fw, grid = caps & 0xFFFF, (caps >> 16) & 0xFF
        ok = fw == 288
        fails += not ok
        print(
            f"  node {i} A_CAPS {caps:#018x}  flit_width={fw} grid={grid}  {'ok' if ok else 'WRONG'}"
        )

    # 2 ------------------------------------------ write via 0, read via 1..3
    addr = 0x0001_0000
    data = bytes(((i * 7 + 3) ^ (i >> 5)) & 0xFF for i in range(1024))
    t0 = time.monotonic()
    t.write_block(mem[0] + addr, data)
    print(
        f"  wrote {len(data)} B through node 0 at flat {addr:#x} in {time.monotonic() - t0:.1f}s"
    )
    for i in range(1, 4):
        got = t.read_block(mem[i] + addr, len(data))
        ok = got == data
        fails += not ok
        print(
            f"  node {i} reads it back: {'ok' if ok else 'MISMATCH ' + got[:32].hex()}"
        )
    home, inch = rotate(addr)
    word = t.backdoor_read(home, inch >> 6)
    ok = word == data[:64]
    fails += not ok
    print(
        f"  DRAM channel {home} word {inch >> 6:#x} (write-through): {'ok' if ok else 'MISMATCH ' + word[:16].hex()}"
    )

    # 3 --------------------------------------------- mover copy across channels
    src, dst = 0x0010_0000, 0x0020_0000
    stride, per = 1 << ILV_LG, 16  # 4 channels x 16 words of 32 B
    pattern = {}
    for c in range(4):
        blob = bytes(((c * 31 + k) * 13 + 5) & 0xFF for k in range(per * 32))
        pattern[c] = blob
        t.write_block(mem[0] + src + c * stride, blob)
    print(
        f"  source: 4 x {per} words at {src:#x} + c*{stride:#x} (homes "
        f"{[rotate(src + c * stride)[0] for c in range(4)]})"
    )
    walk = [(4, stride), (per, 32)]
    prog = mover.copy(
        mover.Walker(src, dims=walk), mover.Walker(dst, dims=walk), ewidth=mover.W32
    )
    stat0 = mover.status(t.read64(ctrl[0] + mover.AUX_STAT))
    t0 = time.monotonic()
    mover.issue(t, prog, ctrl[0])
    for _ in range(200):
        st = mover.status(t.read64(ctrl[0] + mover.AUX_STAT))
        if not st["busy"]:
            break
        t.run(500)
    else:
        print("  mover: still busy after 100k cycles")
        fails += 1
    moved = (st["moves"] - stat0["moves"]) & 0xFF_FFFF
    print(
        f"  mover on node 0: fault={st['fault']} moves+={moved} reads+={(st['reads'] - stat0['reads']) & 0xFFFF} "
        f"writes+={(st['writes'] - stat0['writes']) & 0xFFFF}  ({time.monotonic() - t0:.1f}s)"
    )
    fails += st["fault_code"] != 0
    for c in range(4):
        got = t.read_block(mem[2] + dst + c * stride, per * 32)
        ok = got == pattern[c]
        fails += not ok
        print(
            f"  node 2 reads the copy, channel-slice {c}: {'ok' if ok else 'MISMATCH ' + got[:32].hex()}"
        )

    # 4 --------------------------------- a program on node 0's RV64, over the bus
    elf = ROOT / "build" / "rv64" / "hello_kohakuaccel.elf"
    if elf.exists():
        win = rv64load.LoadWindow(t, ctrl[0] + rv64load.WINDOW_OFFSET)
        t0 = time.monotonic()
        info = win.load_elf(elf)
        print(
            f"  loaded in {time.monotonic() - t0:.1f}s ({t.calls} host calls so far)",
            flush=True,
        )
        win.stdin("Kohaku\n")
        print(f"  stdin queued; status {win.status():#x}", flush=True)
        r = win.run(
            expect="Nice to meet you, Kohaku", timeout=300, poll=lambda: t.run(2000)
        )
        print(
            f"  node 0 RV64 loaded through +0x8000: text {info['text']} B, spad {info['spad']} B "
            f"({time.monotonic() - t0:.1f}s incl. run)"
        )
        print(f"  console: {r['console'].strip()!r}")
        print(
            f"  exit {r['exit']:#x} halted={r['halted']} status={r['status']:#x} in {r['seconds']:.1f}s  "
            f"{'ok' if r['ok'] and r['exit'] == 0 else 'WRONG'}"
        )
        fails += not (r["ok"] and r["exit"] == 0)
    else:
        print(f"  (no {elf}: RV64 program step skipped)")
        fails += 1

    # 5 ------------------------------------------------------------- decerr
    s = t.status()
    print(
        f"  station DECERR {s['decerr']:#x}; sys cycles {s['sys']}, ctrl cycles {s['ctrl']}, host calls {t.calls}"
    )
    fails += s["decerr"] != 0
    t.close()
    print("PASS" if not fails else f"FAIL ({fails})")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
