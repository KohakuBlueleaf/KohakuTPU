"""`khs_fp32_sfu`'s coefficients, read out of the RTL that uses them.

`TAB[fsel][idx]` is `(C0, C1, C2)`. The numbers are PARSED from
`src/kohakumpe/simd/generated/khs_seed_tab.v` rather than copied beside it, so
the model and the lane cannot hold different tables -- there is one file.

Regenerate both with `python tests/pe/tools/khs_seed_emit.py`.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]
SRC = ROOT / "src" / "kohakumpe" / "simd" / "generated" / "khs_seed_tab.v"

LEN = (512, 512, 512, 1024)
STRIDE = 1024
C0_W, C1_W, C2_W = 34, 24, 16

#: THREE ARRAYS, ONE PER COEFFICIENT -- the RTL cannot hold them as one 74-bit
#: word without falling out of block RAM, so neither can this.
_BANKS = (("m0", C0_W), ("m1", C1_W), ("m2", C2_W))


def _sx(v, w):
    return v - (1 << w) if v >> (w - 1) else v


def _load():
    text = SRC.read_text(encoding="utf-8")
    tab = [[[0, 0, 0] for _ in range(n)] for n in LEN]
    for k, (nm, w) in enumerate(_BANKS):
        row = re.compile(rf"{nm}\[\s*(\d+)\]\s*=\s*{w}'h([0-9a-fA-F]+)\s*;")
        for m in row.finditer(text):
            fsel, idx = divmod(int(m.group(1)), STRIDE)
            if idx >= LEN[fsel]:
                continue
            tab[fsel][idx][k] = _sx(int(m.group(2), 16), w)
    return [[tuple(t) for t in bank] for bank in tab]


TAB = _load()
