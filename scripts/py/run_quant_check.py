"""Check mx_quant.v against kohakutpu.hw.mxfp7, element by element.

    python scripts/py/run_quant_check.py

The Verilog bench only records what the circuit produced. The expected values
come from the compiler's model, which is the spec, so this is a genuine
cross-check between an implementation and its specification rather than
between two implementations by the same hand.
"""

import os
import pathlib
import shutil
import subprocess
import sys

import numpy as np
from kohakutpu.hw import mxfp7

# parents[2], not parent.parent: this file is scripts/py/, so two levels up is
# the repo root and one is scripts/, where src/ does not exist.
ROOT = pathlib.Path(__file__).resolve().parents[2]
VIVADO = pathlib.Path(os.environ.get("VIVADO_BIN", r"D:\Xilinx\Vivado\2024.2\bin"))
ENTRIES = 256
SRC = ["src/kohakumas/mx_quant.v", "tests/mas/mx_quant_tb.v"]


def operands(entries, seed=1):
    """Awkward on purpose: wide exponents, subnormals, zeros, and the extremes."""
    rng = np.random.default_rng(seed)
    x = rng.standard_normal((entries * 4, 32)) * (
        10.0 ** rng.integers(-6, 5, (entries * 4, 1))
    )
    x[0] = 0.0
    x[1, :6] = [0.0, 65504.0, -65504.0, 6.1e-5, -6.0e-8, 1.0]
    x[2] = 6.0e-8  # all subnormal
    x[3, 0] = 65504.0  # one huge outlier
    return x.astype(np.float16)


def pack_words(h):
    """Entry g is 4 lanes x 32 FP16, lane-major, 16 values per 256-bit word."""
    flat = h.reshape(-1, 4 * 32)
    words = []
    for row in flat:
        v = row.view(np.uint16)
        for w in range(8):
            word = 0
            for i in range(16):
                word |= int(v[w * 16 + i]) << (i * 16)
            words.append(word)
    return words


def unpack_q(word, b_layout):
    """The 32 signed int7 slots of one operand word, in (lane, k%8) order."""
    out = np.zeros((4, 8), dtype=np.int64)
    for slot in range(32):
        raw = (word >> (255 - slot * 7 - 6)) & 0x7F
        val = raw - 128 if raw & 0x40 else raw
        lane, kk = (slot % 4, slot // 4) if b_layout else (slot // 8, slot % 8)
        out[lane, kk] = val
    return out


def unpack_scales(word):
    return [(word >> (31 - i * 8 - 7)) & 0xFF for i in range(4)]


def main():
    work = ROOT / "build" / "quant_check"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    h = operands(ENTRIES)
    (work / "quant_in.hex").write_text(
        "\n".join(f"{w:064x}" for w in pack_words(h)) + "\n"
    )

    env = {**os.environ, "PATH": str(VIVADO) + ";" + os.environ["PATH"]}

    def run(cmd):
        r = subprocess.run(
            [str(VIVADO / cmd[0])] + cmd[1:],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode:
            print(r.stdout[-4000:], r.stderr[-2000:])
            sys.exit(f"failed: {cmd[0]}")
        return r.stdout

    (work / "f.f").write_text("\n".join(str(ROOT / p) for p in SRC) + "\n")
    run(["xvlog.bat", "-sv", "-work", "w", "-f", "f.f"])
    run(["xelab.bat", "-timescale", "1ns/1ps", "w.mx_quant_tb", "-s", "tb"])
    out = run(["xsim.bat", "tb", "-runall"])
    if "QUANT-DONE" not in out:
        sys.exit("bench did not finish:\n" + out[-3000:])

    got = [int(x, 16) for x in (work / "quant_out.hex").read_text().split()]

    q_want, es_want, m8_want = mxfp7.quantise_fp16(h.astype(np.float32))
    q_want = q_want.reshape(ENTRIES, 4, 32)
    field_want = ((es_want + mxfp7.SBIAS) << 3) | (m8_want - 8)
    field_want = field_want.reshape(ENTRIES, 4)

    bad_q = bad_s = 0
    first = []
    for e in range(ENTRIES):
        blay = bool((got[e * 5 + 4] >> 7) & 1)
        for w in range(4):
            q = unpack_q(got[e * 5 + w], blay)
            for lane in range(4):
                for kk in range(8):
                    want = q_want[e, lane, w * 8 + kk]
                    if q[lane, kk] != want:
                        bad_q += 1
                        if len(first) < 8:
                            first.append(
                                f"    entry {e} lane {lane} k {w * 8 + kk}: "
                                f"rtl {q[lane, kk]} model {want}"
                            )
        for lane, s in enumerate(unpack_scales(got[e * 5 + 0])):
            if s != field_want[e, lane]:
                bad_s += 1
                if len(first) < 8:
                    first.append(
                        f"    entry {e} lane {lane} scale: "
                        f"rtl {s:#04x} model {field_want[e, lane]:#04x}"
                    )

    total = ENTRIES * 4 * 32
    print(f"  significands: {total - bad_q}/{total} match")
    print(f"  scale fields: {ENTRIES * 4 - bad_s}/{ENTRIES * 4} match")
    for line in first:
        print(line)
    ok = bad_q == 0 and bad_s == 0
    print("  PASS" if ok else "  FAIL")
    shutil.rmtree(work, ignore_errors=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
