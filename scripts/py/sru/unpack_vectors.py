"""FP16 <-> E8M15 vectors for the SRU's format benches, from the independent
format oracle (scripts/py/sru/oracle.py; NUM.F16.001-002, ISA.SR.042, ISA.SR.047).

    python scripts/py/sru/unpack_vectors.py

build/sru/unpack.hex and unpack_acc.hex: 4,096 lines `in want` each, every
FP16 bit pattern 16 to a 256-bit word and the 16 E8M15 they convert to,
for tests/sru/stream/sru_unpack_tb.v.

build/sru/e8f16_acc.hex: lines `in want`, 16 E8M15 and the accumulator FP16
for tests/sru/stream/sru_e8f16_tb.v.
"""

import argparse
import pathlib
import random

import oracle

ROOT = pathlib.Path(__file__).resolve().parents[3]


def words(conv, width_in, width_out, values):
    lines = []
    for k in range(0, len(values), 16):
        wi = wo = 0
        for j, v in enumerate(values[k : k + 16]):
            wi |= v << (width_in * j)
            wo |= conv(v) << (width_out * j)
        lines.append(f"{wi:0{width_in * 4}x} {wo:0{width_out * 4}x}")
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "build" / "sru"))
    args = ap.parse_args()

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    every = list(range(1 << 16))
    (out / "unpack.hex").write_text(
        "\n".join(words(oracle.f16_to_e8_ieee, 16, 24, every)) + "\n"
    )
    (out / "unpack_acc.hex").write_text(
        "\n".join(words(oracle.f16_to_e8_acc, 16, 24, every)) + "\n"
    )

    rng = random.Random(3)
    vals = []
    for e in range(256):
        ms = (
            range(1 << 15) if 100 <= e <= 150 else range(rng.randrange(97), 1 << 15, 97)
        )
        for m in ms:
            vals.append((rng.getrandbits(1) << 23) | (e << 15) | m)
    vals += [0] * (-len(vals) % 16)
    (out / "e8f16_acc.hex").write_text(
        "\n".join(words(oracle.e8_to_f16_acc, 24, 16, vals)) + "\n"
    )
    print(f"{out} unpack.hex unpack_acc.hex 65536 values, e8f16_acc.hex {len(vals)}")


if __name__ == "__main__":
    main()
