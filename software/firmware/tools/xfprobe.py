"""The node processor's view of the transform bank and the mover status word,
on the simulated card.

    python software/firmware/tools/xfprobe.py --build build/v9v2b/vlt_card_v9_1n_v2

Boots `build/fw/xfprobe.elf` (python software/firmware/build.py xfprobe) on node 0 and
checks what it read:
  1. MVSTAT bit 33 (configuration-queue room) is set on an idle mover, busy and
     fault clear;
  2. XF_DATA at each id's geometry register is that occupant's
     {out words, in bits} (`xform_bank.v`), zero for an id naming none;
  3. the bank's fault word is clear.
"""

import argparse
import pathlib
import sys
import time

from kohakuaccel.driver.node.boot import BootArgs, NodeBoot
from kohakuaccel.driver.transport.verilator import VerilatorTransport
from kohakutpu.driver.clock.card import load_board
from kohakutpu.driver.host import board_map

ROOT = pathlib.Path(__file__).resolve().parents[3]
PROBE = 0x0030_0000
#: {8'd0, out words, in bits} per id: bypass, quantise, GT4, none.
GEOMETRY = [4 << 16 | 1024, 4 << 16 | 2048, 4 << 16 | 1024, 0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", required=True)
    ap.add_argument("--board", default="multimesh_v9")
    ap.add_argument("--load", choices=("burn", "axi"), default="burn")
    a = ap.parse_args()
    m = board_map(load_board(a.board))
    host, glob = m["mem"][0] + PROBE, m["dram"][0] + PROBE

    t = VerilatorTransport(build_dir=a.build)
    t.write_block(host + 0x200, bytes(64))
    node = NodeBoot(t, m["ctrl"][0], 0, sim=t)
    node.load(ROOT / "build/fw/xfprobe.elf", BootArgs(queue=glob, mesh=0), a.load)
    text, t0 = "", time.monotonic()
    while time.monotonic() - t0 < 120:
        text += node.console()
        st = node.status()
        if st["exited"] or st["halted"]:
            break
        t.run(2000)
    text += node.console()
    print("  console:", text.strip().replace("\n", " | "))
    fails = not (st.get("exited") and st.get("exit") == 0)

    res = t.read_block(host + 0x200, 48)
    w = [int.from_bytes(res[k : k + 8], "little") for k in range(0, 48, 8)]
    mv = w[0]
    ok = (mv >> 33) & 1 == 1 and (mv >> 32) & 1 == 0 and (mv >> 28) & 0xF == 0
    fails += not ok
    print(
        f"  MVSTAT {mv:#x}: room {(mv >> 33) & 1} busy {(mv >> 32) & 1}  {'ok' if ok else 'WRONG'}"
    )
    for i, want in enumerate(GEOMETRY):
        ok = w[1 + i] == want
        fails += not ok
        print(
            f"  id {i} geometry {w[1 + i]:#010x} want {want:#010x}  {'ok' if ok else 'WRONG'}"
        )
    ok = w[5] == 0
    fails += not ok
    print(f"  bank fault word {w[5]:#x}  {'ok' if ok else 'WRONG'}")
    t.close()
    print("PASS" if not fails else f"FAIL ({fails})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
