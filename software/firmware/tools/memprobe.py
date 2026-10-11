"""Measure the node processor's memory paths on the simulated card.

    python software/firmware/tools/memprobe.py [--node 0] [--build build/sw/vlt_card_v8t8_2n]

Boots `build/fw/memprobe.elf` (python software/firmware/build.py memprobe) on one node
and checks, each as a fact the software stack is built on:
  1. host-written DRAM reads back on the processor through the uncached alias
     (bit 38) at the UNIT-GLOBAL address, and through the cached range;
  2. four 8-byte uncached stores into one 32-byte line all land (strobes), and
     one store into a line leaves that line's other three words alone;
  3. what an uncached load, an uncached store and a cached line fill cost.
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
#: Below the model's 16 MB of DRAM, clear of card_run.py's 0x1_0000..0x22_0000.
PROBE = 0x0030_0000


def pattern(i: int) -> int:
    return 0x1111_0000_0000_0000 * (i + 1) + i


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", type=int, default=0)
    ap.add_argument("--build", default=str(ROOT / "build/sw/vlt_card_v8t8_2n"))
    ap.add_argument("--load", choices=("burn", "axi"), default="burn")
    a = ap.parse_args()
    m = board_map(load_board("multimesh_v8t8"))
    i = a.node
    host, glob = m["mem"][i] + PROBE, m["dram"][i] + PROBE

    t = VerilatorTransport(build_dir=a.build)
    print(f"model up: {t.ready}")
    fails = 0
    words = b"".join(pattern(k).to_bytes(8, "little") for k in range(8))
    t.write_block(host, words)
    t.write_block(host + 0x100, bytes(32))
    t.write_block(host + 0x120, b"\xaa" * 32)
    t.write_block(host + 0x200, bytes(64))

    node = NodeBoot(t, m["ctrl"][i], i, sim=t)
    info = node.load(
        ROOT / "build/fw/memprobe.elf", BootArgs(queue=glob, mesh=i), a.load
    )
    print(
        f"  loaded ({info['mode']}, {info['load_seconds']:.1f}s): text {info['text']} B"
    )
    text, t0 = "", time.monotonic()
    while time.monotonic() - t0 < 240:
        text += node.console()
        st = node.status()
        if st["exited"] or st["halted"]:
            break
        t.run(2000)
    text += node.console()
    print("  console:", text.strip().replace("\n", " | "))
    print(f"  status {st}")
    fails += not (st.get("exited") and st.get("exit") == 0)

    res = t.read_block(host + 0x200, 40)
    ok, t_ld, t_st, t_cl, okc = (
        int.from_bytes(res[k : k + 8], "little") for k in range(0, 40, 8)
    )
    print(f"  uncached readback mask {ok:#x}, cached {okc:#x} (want 0xff)")
    fails += ok != 0xFF
    line = t.read_block(host + 0x100, 32)
    want = b"".join((0xC0DE_0000_0000_0000 | k).to_bytes(8, "little") for k in range(4))
    strobes = line == want
    print(
        f"  four stores into one line: {'all land' if strobes else 'LOST ' + line.hex()}"
    )
    fails += not strobes
    mark = t.read_block(host + 0x120, 32)
    alone = mark == b"\xaa" * 8 + (0xBEEF).to_bytes(8, "little") + b"\xaa" * 16
    print(
        f"  one store leaves its line's neighbours: {'yes' if alone else 'NO ' + mark.hex()}"
    )
    fails += not alone
    print(
        f"  cycles: uncached load {t_ld / 64:.1f}, uncached store {t_st / 64:.1f}, "
        f"cached load (16 fills / 64) {t_cl / 64:.1f}"
    )
    t.close()
    print("PASS" if not fails else f"FAIL ({fails})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
