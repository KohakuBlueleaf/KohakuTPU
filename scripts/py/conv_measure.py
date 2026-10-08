"""The conv figures in sdxl-requirements.md §5.6, §5.8 and §9.2, on the models.

    python scripts/py/conv_measure.py [stride2] [upsample] [concat] [groupnorm]

`stride2` and `upsample` price the lowered conv on the cost model: the im2col at
`cost.quantise_cycles`, the matmul plan at `cost.time`. `concat` runs the
channel concat and slice as mover moves on `model.run_move` and compares the
bytes with numpy's packing. `groupnorm` prints the channels-last group runs.
"""

import sys

import numpy as np
from kohakuaccel.package import mover as PM
from kohakutpu.cost import quantise_cycles
from kohakutpu.cost import time as cost_time
from kohakutpu.model import SimDevice, run_move
from kohakutpu.ops.conv2d import Geometry, im2col_moves
from kohakutpu.ops.matmul import matmul

from kohakutpu import layout as LO


def conv_cost(g: Geometry, cout: int, gm: int, gn: int) -> tuple:
    """(im2col entries, im2col cycles, matmul cycles) of one lowered conv."""
    dev = SimDevice(size=4096 << 20)
    gt = g.groups(gm)
    lay = LO.MxEntry(gt, 1, 0)
    col = dev.empty((g.rows, g.k), lay)
    wt = dev.tensor(np.zeros((cout, g.k), np.float16))
    moves = im2col_moves(g, gt, 0x1000_0000, 0x2000_0000)
    entries = g.rows * g.k // (LO.LANES * LO.KBLOCK)
    im = quantise_cycles(len(moves), entries)
    gn = max(1, min(gn, -(-cout // LO.LANES)))
    mm = cost_time(matmul.plan(col, wt, gm=gt, gn=gn, nk=1), dev.machine).cycles
    return entries, im, mm


def stride2() -> None:
    print("## stride 2 against dense-and-discard")
    for h, w, cin, cout, gm, gn in (
        (16, 16, 32, 32, 8, 8),
        (32, 32, 32, 64, 8, 8),
        (64, 64, 32, 32, 16, 16),
    ):
        s = conv_cost(Geometry(h, w, cin, stride=2), cout, gm, gn)
        d = conv_cost(Geometry(h, w, cin), cout, gm, gn)
        ts, td = s[1] + s[2], d[1] + d[2]
        print(
            f"{h}x{w}x{cin}->{cout} gm{gm} gn{gn}: strided entries {s[0]} im2col {s[1]} "
            f"matmul {s[2]} total {ts}; dense entries {d[0]} im2col {d[1]} matmul "
            f"{d[2]} total {td}; matmul x{d[2] / s[2]:.2f}, total x{td / ts:.2f}"
        )


def upsample() -> None:
    print("## upsample: four 2x2 classes against 3x3 over the 2x activation")
    for h, w, cin, cout, gm, gn in (
        (8, 8, 32, 32, 8, 8),
        (16, 16, 32, 64, 8, 8),
        (32, 32, 64, 32, 16, 16),
    ):
        f = [0, 0, 0]
        for c in range(4):
            g = Geometry(h, w, cin, kh=2, kw=2, sy=c // 2, sx=c % 2, out=(h, w))
            got = conv_cost(g, cout, gm, gn)
            f = [a + b for a, b in zip(f, got, strict=True)]
        d = conv_cost(Geometry(2 * h, 2 * w, cin), cout, gm, gn)
        tf, td = f[1] + f[2], d[1] + d[2]
        print(
            f"{h}x{w}x{cin}->{cout}: fused entries {f[0]} im2col {f[1]} matmul {f[2]} "
            f"total {tf}; dense entries {d[0]} im2col {d[1]} matmul {d[2]} total {td}; "
            f"matmul x{d[2] / f[2]:.2f}, total x{td / tf:.2f}"
        )


def concat_slice() -> None:
    print("## channels-last concat and slice, by mover COPY, against numpy")
    rng = np.random.default_rng(5)
    for h, w, ca, cb in (
        (32, 32, 1280, 1280),
        (64, 64, 640, 640),
        (128, 128, 320, 320),
        (64, 64, 640, 1280),
        (8, 8, 32, 64),
    ):
        dev = SimDevice(size=512 << 20)
        a = rng.standard_normal((h, w, ca)).astype(np.float16)
        b = rng.standard_normal((h, w, cb)).astype(np.float16)
        img = LO.PadHWC(h + 2, w + 2, ca + cb, 1)
        px = img.cp * 2
        dst = dev.alloc(img.nbytes(None))
        row = img.wp * px
        writes = [
            PM.fill((dst, [(img.hp, row), (row // PM.WORD_BYTES, PM.WORD_BYTES)]))
        ]
        origin = dst + (img.wp + 1) * px
        moves = 0
        for arr, off in ((a, 0), (b, ca)):
            c = arr.shape[2]
            src = dev.put(arr, LO.Flat()).addr
            per = c * 2 // PM.WORD_BYTES
            writes.append(
                PM.move(
                    PM.COPY,
                    (src, [(h, w * c * 2), (w, c * 2), (per, PM.WORD_BYTES)]),
                    (origin + off * 2, [(h, row), (w, px), (per, PM.WORD_BYTES)]),
                )
            )
            moves += 1
        for wr in writes:
            run_move(wr, dev.card.mem)
        cat = dev.read(dst, img.nbytes(None)) == img.pack(np.concatenate([a, b], 2))

        # The slice: the low `ca` channels of the concatenated image, to Flat.
        out = dev.alloc(h * w * ca * 2)
        per = ca * 2 // PM.WORD_BYTES
        run_move(
            PM.move(
                PM.COPY,
                (origin, [(h, row), (w, px), (per, PM.WORD_BYTES)]),
                (out, [(h, w * ca * 2), (w, ca * 2), (per, PM.WORD_BYTES)]),
            ),
            dev.card.mem,
        )
        sl = dev.read(out, h * w * ca * 2) == LO.Flat().pack(a)
        print(
            f"{h}x{w} {ca}+{cb}: concat = fill + {moves} COPY, byte-identical {cat}; "
            f"slice = 1 COPY, byte-identical {sl}"
        )


def groupnorm() -> None:
    print("## a GroupNorm(32, C) group in channels-last: elements per pixel run")
    for c in (320, 640, 1280, 512, 256, 128, 1024):
        g = c // 32
        print(
            f"C={c}: {g} channels a group = {g * 2} B a pixel; whole 32-B words: "
            f"{g * 2 % PM.WORD_BYTES == 0}; whole 16-element sub-rows: {g % 16 == 0}"
        )


PARTS = {
    "stride2": stride2,
    "upsample": upsample,
    "concat": concat_slice,
    "groupnorm": groupnorm,
}


if __name__ == "__main__":
    for p in sys.argv[1:] or list(PARTS):
        PARTS[p]()
