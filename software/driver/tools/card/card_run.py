"""Drive a simulated card through the driver's own seam.

    python software/driver/tools/card/card_run.py       # card_v8t8_2n, nodes 0,1
    python software/driver/tools/card/card_run.py --nodes 0,1,2,3 --build build/vlt_<card>

Checks, each a fact about the image:
  1. every node's agent answers A_CAPS through its control window;
  2. a block written through the first node's memory window reads back through
     every other node, and the DRAM behind the Xache holds it (backdoor);
  3. the first node's mover copies 64 words laid out across all four channels
     and the last node reads the copy back;
  4. hello_kohakuaccel runs on every node's RV64 through its load window;
  5. the station's DECERR counter is still zero.
"""

import argparse
import pathlib
import sys
import time

from kohakuaccel.driver.device import mover, rv64load
from kohakuaccel.driver.device.registers import A_CAPS
from kohakuaccel.driver.transport.simdram import SimDram
from kohakuaccel.driver.transport.verilator import VerilatorTransport
from kohakutpu.driver.clock.card import load_board
from kohakutpu.driver.host import board_map

ILV_LG, HOME_LSB, NHOME_LG = 14, 32, 2
ROOT = pathlib.Path(__file__).resolve().parents[4]


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


def fast_dram_check(t, board: dict, mem, nodes, size: int) -> int:
    """SimDram against the AXI path, through a WARM Xache: the lines are first
    written and read over AXI so the array holds them, then rewritten through the
    fast path, so a missed invalidation reads back the old bytes. Unaligned and
    across a 16 KB home boundary. Returns the failures."""
    fast = SimDram(t, board, mem, size)
    fails = 0
    base = mem[nodes[0]] + 0x0003_C000 - 0x100  # 256 B before a home boundary

    def pattern(seed: int, n: int) -> bytes:
        return bytes(((k * 29 + seed) ^ (k >> 7)) & 0xFF for k in range(n))

    old, new = pattern(1, 4096), pattern(2, 4096)
    t.write_block(base, old)
    warm = t.read_block(base, 4096) == old  # fills the Xache with `old`
    fast.write_block(base + 8, new[8:4000])  # unaligned head and tail
    want = old[:8] + new[8:4000] + old[4000:]
    for i in nodes:
        got = t.read_block(mem[i] + (base - mem[nodes[0]]), 4096)
        ok = warm and got == want
        fails += not ok
        print(f"  fast write, AXI read via node {i}: {'ok' if ok else 'MISMATCH'}")
    t.write_block(base, old)
    ok = fast.read_block(base, 4096) == old
    fails += not ok
    print(f"  AXI write, fast read: {'ok' if ok else 'MISMATCH'}  ({fast!r})")
    return fails


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", default="multimesh_v8t8")
    ap.add_argument("--build", default=str(ROOT / "build" / "vlt_card_v8t8_2n"))
    ap.add_argument("--nodes", default="0,1")
    ap.add_argument("--native", action="store_true")
    ap.add_argument("--settle", type=int, default=20000)
    # axi: stream through the load window (what the card does); burn: write the
    # imem/spad arrays directly (the model only, sim/verilator/card.vlt).
    ap.add_argument("--load", choices=("axi", "burn"), default="axi")
    ap.add_argument("--fast-dram", action="store_true", help="check transport.simdram")
    a = ap.parse_args()
    nodes = [int(x) for x in a.nodes.split(",")]

    m = board_map(load_board(a.board))
    ctrl, mem = m["ctrl"], m["mem"]
    fails = 0

    t0 = time.monotonic()
    t = VerilatorTransport(build_dir=a.build, wsl=not a.native, settle=a.settle)
    print(f"model up in {time.monotonic() - t0:.1f}s: {t.ready}")

    # 1 ---------------------------------------------------------------- caps
    for i in nodes:
        caps = t.read64(ctrl[i] + A_CAPS)
        fw = caps & 0xFFFF
        ok = fw == 288
        fails += not ok
        print(
            f"  node {i} A_CAPS {caps:#018x}  flit_width={fw}  {'ok' if ok else 'WRONG'}"
        )

    # 2 ------------------------------------ write via the first, read via the rest
    addr = 0x0001_0000
    data = bytes(((i * 7 + 3) ^ (i >> 5)) & 0xFF for i in range(1024))
    t0 = time.monotonic()
    t.write_block(mem[nodes[0]] + addr, data)
    print(
        f"  wrote {len(data)} B through node {nodes[0]} at flat {addr:#x} in {time.monotonic() - t0:.1f}s"
    )
    for i in nodes:
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
        f"  DRAM channel {home} word {inch >> 6:#x}: {'ok' if ok else 'MISMATCH ' + word[:16].hex()}"
    )

    # 3 --------------------------------------------- mover copy across channels
    src, dst = 0x0010_0000, 0x0020_0000
    stride, per = 1 << ILV_LG, 16  # 4 channels x 16 words of 32 B
    pattern = {}
    for c in range(4):
        blob = bytes(((c * 31 + k) * 13 + 5) & 0xFF for k in range(per * 32))
        pattern[c] = blob
        t.write_block(mem[nodes[0]] + src + c * stride, blob)
    walk = [(4, stride), (per, 32)]
    prog = mover.copy(
        mover.Walker(src, dims=walk), mover.Walker(dst, dims=walk), ewidth=mover.W32
    )
    c0 = ctrl[nodes[0]]
    stat0 = mover.status(t.read64(c0 + mover.AUX_STAT))
    t0 = time.monotonic()
    mover.issue(t, prog, c0)
    for _ in range(200):
        st = mover.status(t.read64(c0 + mover.AUX_STAT))
        if not st["busy"]:
            break
        t.run(500)
    else:
        print("  mover: still busy after 100k cycles")
        fails += 1
    moved = (st["moves"] - stat0["moves"]) & 0xFF_FFFF
    print(
        f"  mover on node {nodes[0]}: fault={st['fault']} moves+={moved} ({time.monotonic() - t0:.1f}s)"
    )
    fails += st["fault_code"] != 0
    for c in range(4):
        got = t.read_block(mem[nodes[-1]] + dst + c * stride, per * 32)
        ok = got == pattern[c]
        fails += not ok
        print(
            f"  node {nodes[-1]} reads the copy, slice {c}: {'ok' if ok else 'MISMATCH ' + got[:32].hex()}"
        )

    # 4 ------------------------------------------ a program on every node's RV64
    elf = ROOT / "build" / "rv64" / "hello_kohakuaccel.elf"
    for i in nodes:
        if not elf.exists():
            print(f"  (no {elf}: RV64 step skipped)")
            fails += 1
            break
        win = rv64load.LoadWindow(t, ctrl[i] + rv64load.WINDOW_OFFSET)
        t0 = time.monotonic()
        if a.load == "burn":
            cpu = [
                s
                for s, v, _, _ in t.arrays(f"u_mesh{i}.u_mag.u_pe.u_cpu")
                if v == "spad"
            ]
            win.burn_elf(elf, t, cpu[0])
        else:
            win.load_elf(elf)
        t_load = time.monotonic() - t0
        win.stdin("Kohaku\n")
        r = win.run(
            expect="Nice to meet you, Kohaku", timeout=300, poll=lambda: t.run(2000)
        )
        ok = r["ok"] and r["exit"] == 0
        fails += not ok
        print(
            f"  node {i} RV64: console {r['console'].strip()!r} exit {r['exit']:#x} "
            f"(load {a.load} {t_load:.1f}s, total {time.monotonic() - t0:.1f}s)  "
            f"{'ok' if ok else 'WRONG'}"
        )

    # 5 --------------------- the host fast path, against the AXI path and the Xache
    if a.fast_dram:
        fails += fast_dram_check(t, load_board(a.board), mem, nodes, m["size"])

    # 6 ------------------------------------------------------------- decerr
    s = t.status()
    print(
        f"  station DECERR {s['decerr']:#x}; sys cycles {s['sys']}, host calls {t.calls}"
    )
    fails += s["decerr"] != 0
    t.close()
    print("PASS" if not fails else f"FAIL ({fails})")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
