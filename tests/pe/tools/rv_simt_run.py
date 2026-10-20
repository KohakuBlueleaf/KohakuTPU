"""Assemble a shader, model it, and run it on the SIMT PE's machine.

    python tests/pe/tools/rv_simt_run.py shader.s
    python tests/pe/tools/rv_simt_run.py shader.s --lanes 8 --waves 16

It writes the five image files the bench reads, runs the golden model to produce
the expected final DRAM state, then runs the RTL and compares. The witness is
the DUMPED STATE, not a `$display` line -- so any kernel the toolchain can emit
runs without touching the `.v`.

A shader must end in `ecall`; lane 0 of `a0` is the halt word.
"""

import argparse
import os
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import rv_simt_asm  # noqa: F401
from rv_asm import AsmError, assemble, to_hex
from rv_simt_model import DRAM_BASE, MASK, GpuMachine

ROOT = pathlib.Path(__file__).resolve().parents[3]
# KOHAKU_PE_DIR moves the image files, and xsim.py reads the same variable, so
# runs with their own directory can go side by side.
BUILD = pathlib.Path(os.environ.get("KOHAKU_PE_DIR", ROOT / "tests" / "pe" / "build"))
XSIM = ROOT / "scripts" / "py" / "xsim.py"

DRAM_WORDS = 4096
SPAD_WORDS = 2048


def dram_ramp(n=DRAM_WORDS):
    return [(0x1234_0000 + 7 * i) & MASK for i in range(n)]


def dram_hash(n=DRAM_WORDS):
    """Pseudo-random words: gather indices and reduction data with no order."""
    out = []
    for i in range(n):
        z = (i * 0x9E3779B1 + 0x7F4A7C15) & MASK
        z ^= z >> 15
        z = (z * 0x2C1B3C6D) & MASK
        z ^= z >> 12
        out.append(z)
    return out


DRAM_FILLS = {"zero": lambda: [0] * DRAM_WORDS, "ramp": dram_ramp, "hash": dram_hash}


def checksum(words):
    """The bench's own DRAM checksum: sum of word * (index + 1)."""
    s = 0
    for i, v in enumerate(words):
        s = (s + v * (i + 1)) & MASK
    return s


def write_user(src, arg, dram_init, lanes, waves, ctl, launch=1, has_fsfu=False):
    words, _ = assemble(src, base=0)

    m = GpuMachine(
        lanes=lanes,
        waves=waves,
        imem_words=4096,
        ctl=ctl,
        nlive=launch,
        has_fsfu=has_fsfu,
    )
    m.imem[: len(words)] = words
    for i, v in enumerate(dram_init):
        if v:
            m.dram[DRAM_BASE + 4 * i] = v & MASK
    trace, cause, hword = m.run()

    dfin = [m.dram.get(DRAM_BASE + 4 * i, 0) for i in range(DRAM_WORDS)]
    d = BUILD / "simt" / "user"
    d.mkdir(parents=True, exist_ok=True)
    (d / "prog.hex").write_text(to_hex(words))
    (d / "dram.hex").write_text(to_hex(dram_init))
    (d / "dfin.hex").write_text(to_hex(dfin))
    (d / "spad.hex").write_text(to_hex([0] * 64))
    # meta[4] is the wave count the bench kicks with -- the kick's `op` field.
    meta = [cause, hword, len(trace), checksum(dfin), launch, arg, len(words), 0]
    (d / "meta.hex").write_text(to_hex(meta))

    print(
        "  assembled %d words; the model retires %d step(s) and halts with "
        "cause %d, a0 = 0x%08x" % (len(words), len(trace), cause, hword)
    )
    print(
        "  model memory: %d request(s) over %d gather(s), %d line fill(s)"
        % (sum(len(a) for _, a in m.reqs), len(m.reqs), m.fills_issued())
    )
    return m


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("source", help="a shader .s file")
    ap.add_argument("--arg", type=lambda s: int(s, 0), default=0)
    ap.add_argument("--lanes", type=int, default=8)
    ap.add_argument(
        "--waves", type=int, default=16, help="wave contexts the build carries"
    )
    ap.add_argument(
        "--launch",
        type=int,
        default=1,
        help="wave contexts this kick starts (the kick's op field)",
    )
    ap.add_argument("--dram", choices=sorted(DRAM_FILLS), default="ramp")
    ap.add_argument("--wall", type=float, default=600.0)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--define", "-d", action="append", default=[])
    # Passed to xsim.py as they are; the mutation witnesses build a broken copy.
    ap.add_argument("--swap", action="append", default=[])
    ap.add_argument("--build-root")
    ap.add_argument("--incdir", action="append", default=[])
    ap.add_argument("--reuse", action="store_true", help="xsim.py --reuse")
    a = ap.parse_args()

    src = pathlib.Path(a.source)
    if not src.is_file():
        print("  no such file: %s" % src)
        return 1

    # Slot 0 is the kick argument, as the control table says; the shader reads
    # its base pointers from here rather than building them from immediates.
    ctl = [a.arg] + [0] * 31
    # The model's seed gate follows the build's: KHT_FSFU defaults to 0 in the bench.
    fsfu = [d.split("=", 1)[1] for d in a.define if d.startswith("KHT_FSFU=")]
    has_fsfu = bool(fsfu) and int(fsfu[-1], 0) > 0
    try:
        write_user(
            src.read_text(),
            a.arg,
            DRAM_FILLS[a.dram](),
            a.lanes,
            a.waves,
            ctl,
            a.launch,
            has_fsfu,
        )
    except AsmError as e:
        print("  assembly failed: %s" % e)
        return 1
    except RuntimeError as e:
        print("  the model never halted: %s" % e)
        print("  every shader must end in `ecall`.")
        return 1

    defines = []
    for d in a.define:
        defines += ["-d", d]
    defines += ["-d", "KHT_LANES=%d" % a.lanes, "-d", "KHT_WAVES=%d" % a.waves]

    cmd = [sys.executable, str(XSIM), "kht_sys", "--wall", str(a.wall)] + defines
    if a.keep:
        cmd.append("--keep")
    if a.reuse:
        cmd.append("--reuse")
    for s in a.swap:
        cmd += ["--swap", s]
    for i in a.incdir:
        cmd += ["--incdir", i]
    if a.build_root:
        cmd += ["--build-root", a.build_root]
    print("  $ " + " ".join(str(x) for x in cmd))
    return subprocess.run(cmd, cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
