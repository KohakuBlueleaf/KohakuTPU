"""Host <-> staging through each node's memory window, on the Verilator card.

    python software/driver/tools/card/staging_check.py [--build build/vlt_card_v8t8_2n]

Writes a pattern to node i's staging aperture ((i+1)<<40 | 1<<39 | i<<36 | off),
reads it back through the window, and reads it back through node i's RV64 (its
uncached load path cannot see staging, so the witness is the model's own
staging array, found by name). Prints PASS/FAIL per node.
"""

import argparse
import pathlib
import sys

from kohakuaccel.driver.transport.verilator import VerilatorTransport

ROOT = pathlib.Path(__file__).resolve().parents[4]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default=str(ROOT / "build" / "vlt_card_v8t8_2n"))
    ap.add_argument("--nodes", default="0,1")
    a = ap.parse_args()
    t = VerilatorTransport(build_dir=a.build, settle=2000)
    fails = 0
    for i in (int(x) for x in a.nodes.split(",")):
        stg = ((i + 1) << 40) | (1 << 39) | (i << 36) | 0x400
        data = bytes(((k * 29 + i * 7 + 1) & 0xFF) for k in range(256))
        try:
            t.write_block(stg, data)
            got = t.read_block(stg, len(data))
        except RuntimeError as e:
            print(f"  node {i}: AXI error {e}")
            fails += 1
            continue
        ok = got == data
        fails += not ok
        print(
            f"  node {i}: window round trip {'ok' if ok else 'MISMATCH ' + got[:16].hex()}"
        )
        # the witness: the bytes must sit in one of node i's staging banks
        banks = [s for s in t.arrays(f"u_mesh{i}.u_mag") if ".u_bank." in s[0] + "."]
        hit = []
        for scope, var, n, ent in banks:
            blob = t.peek(scope, var, 0, n)
            for k in range(0, len(data), ent):
                if data[k : k + ent] in blob:
                    hit.append(
                        (
                            scope.split(".u_mag.")[1],
                            blob.index(data[k : k + ent]) // ent,
                        )
                    )
        ok = len(hit) == len(data) // 32
        fails += not ok
        print(
            f"  node {i}: {len(hit)}/{len(data) // 32} 32-B words found in staging banks {'ok' if ok else 'MISSING'}"
        )
    s = t.status()
    print(f"  decerr {s['decerr']:#x}")
    fails += s["decerr"] != 0
    t.close()
    print("PASS" if not fails else f"FAIL ({fails})")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
