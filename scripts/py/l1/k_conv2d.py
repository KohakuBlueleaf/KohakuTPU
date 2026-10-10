"""Card run of the L1 conv2d 3x3 (`kohakutpu.ir.l1.kernels.conv2d`).

    python scripts/py/l1/k_conv2d.py --build build/v9prof/vlt_card_v9_1n --shape 64,64,128,128 --tile 44,32

Graded against the reference on MXFP7-quantised input and weights.
"""

import argparse
import pathlib
import sys
import time

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from card import L1Card
from check import grade
from kohakutpu.hw.mxfp7 import value_fp16
from kohakutpu.ir.l1 import Program
from kohakutpu.ir.l1.kernels import conv2d as CV


def reference(x, wt, quantised: bool):
    h, w, cin = x.shape
    cout = wt.shape[0]
    wk = np.ascontiguousarray(wt.transpose(0, 2, 3, 1)).reshape(cout, 9 * cin)
    xs = value_fp16(x) if quantised else x.astype(np.float64)
    ws = (value_fp16(wk) if quantised else wk.astype(np.float64)).reshape(
        cout, 3, 3, cin
    )
    xp = np.zeros((h + 2, w + 2, cin))
    xp[1:-1, 1:-1] = xs
    return sum(xp[dy : dy + h, dx : dx + w] @ ws[:, dy, dx, :].T for dy, dx in CV.TAPS)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", required=True)
    ap.add_argument("--shape", action="append", default=[], help="H,W,Cin,Cout")
    ap.add_argument(
        "--tile", action="append", default=[], help="gm,gn[,blocks a chunk]"
    )
    a = ap.parse_args()
    shapes = [tuple(int(v) for v in s.split(",")) for s in a.shape] or [
        (32, 32, 64, 64)
    ]
    tiles = [tuple(int(v) for v in s.split(",")) for s in a.tile] or [(44, 16)]
    card = L1Card(a.build)
    rng = np.random.default_rng(11)
    for h, w, cin, cout in shapes:
        x = (rng.standard_normal((h, w, cin)) * 0.5).astype(np.float16)
        wt = (rng.standard_normal((cout, cin, 3, 3)) * 0.2).astype(np.float16)
        want, model = reference(x, wt, False), reference(x, wt, True)
        for tile in tiles:
            gm, gn = tile[:2]
            t0 = time.monotonic()
            card.top = 0x0010_0000
            prog = Program(card.machine)
            try:
                cbc = tile[2] if len(tile) > 2 else CV.chunk_blocks(cin, gm, gn)
                a_at = card.put(np.frombuffer(CV.pack_input(x, gm, cbc), np.uint8))
                b_at = card.put(np.frombuffer(CV.pack_weights(wt, gn, cbc), np.uint8))
                c_at = card.alloc(CV.geometry(h, w, gm)[2] * CV.LANES * cout * 2)
                where = CV.conv(prog, a_at, b_at, c_at, h, w, cin, cout, gm, gn, cbc)
            except ValueError as exc:
                print(f"conv {h}x{w} {cin}->{cout} tile {gm}x{gn}: {exc}")
                continue
            card.run(prog)
            got = card.run(prog)["cycles"]
            grade(
                f"conv {h}x{w} {cin}->{cout} tile {gm}x{gn}",
                CV.unpack(card.get, where, h, w, cout, gm, gn),
                model,
                want,
            )
            eff = h * w * cin * cout * 9 / (1024 * len(prog.units("MG")) * got)
            print(
                f"conv {h}x{w} {cin}->{cout} tile {gm}x{gn} chunk {cbc}: {got} cycles  "
                f"MAC {eff:.3f}  "
                f"({time.monotonic() - t0:.0f}s)",
                flush=True,
            )
    card.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
