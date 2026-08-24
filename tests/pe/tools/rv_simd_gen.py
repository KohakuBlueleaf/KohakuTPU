"""Build the DSP workload suite: images, golden DRAM, and the kernel's own size.

    python tests/pe/tools/rv_simd_gen.py

Each case becomes tests/pe/build/simd/simdNN/{prog,dram,dfin,meta}.hex, which
tests/pe/tb/rv_simd_tb.v walks in one simulation.

The one thing this does that the other generators do not: **it counts the
kernel's dynamic instructions**, by running the golden model and counting
retirements whose PC falls between the `kern_start` and `kern_end` labels. That
number is the denominator's denominator -- cycles alone cannot say whether a
kernel is slow because it executes many instructions or because it stalls, and
the specialization frontier is an argument about instructions removed.
"""

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import rv_simd_asm  # noqa: F401
import rv_simd_kernels as K
from rv_asm import assemble, to_hex
from rv_gen import SYMS, _sum, zero_regs
from rv_model import DRAM_BASE
from rv_simd_model import VSPAD_BASE, DspMachine

ROOT = pathlib.Path(__file__).resolve().parents[3]

DRAM_WORDS = 4096  # what the bench's AXI RAM covers, in 32-bit words
SPAD_WORDS = 2048
IMEM_WORDS = 2048  # the bench's window; a program past it must FAIL here
VSPAD_ENTRIES = 1024

#: The vector scratchpad's base, so a kernel writes `VSPAD+off` rather than the
#: constant twice.
ALL_SYMS = dict(SYMS, VSPAD=VSPAD_BASE)


def build_one(name, simd):
    """Assemble, model, and return everything a bench row needs."""
    K.SIMD = simd
    src = zero_regs() + K.build_case(name)
    words, syms = assemble(src, base=0, symbols=ALL_SYMS)
    if len(words) > IMEM_WORDS:
        raise SystemExit(
            "case %s is %d words, window is %d" % (name, len(words), IMEM_WORDS)
        )

    # Every case runs on the DSP-enabled machine, scalar ones included: the
    # baselines and the vector kernels must be measured on ONE configuration or
    # the ratio between them is not a speedup.
    m = DspMachine(
        simd=simd,
        vspad_entries=VSPAD_ENTRIES,
        imem_words=IMEM_WORDS,
        spad_words=SPAD_WORDS,
        arg=0,
        coreid=0x11,
    )
    m.imem[: len(words)] = words
    trace, cause, hword = m.run(limit=4_000_000)

    lo, hi = syms["kern_start"], syms["kern_end"]
    kern_instr = sum(1 for pc, _, _ in trace if lo <= pc < hi)

    dfin = [m.dram.get(DRAM_BASE + 4 * i, 0) for i in range(DRAM_WORDS)]
    ck = dfin[K.CK_DRAM // 4]
    return words, dfin, cause, hword, len(trace), kern_instr, ck


def build(outdir, simd):
    outdir = pathlib.Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    index = []
    K.SIMD = simd
    for n, (name, _, note) in enumerate(K.cases_for(simd)):
        words, dfin, cause, hword, retired, kern_instr, ck = build_one(name, simd)

        # The model returns 0 for CTL_CYCLE, so its halt word is 0 by
        # construction. It is recorded rather than checked -- see the suite's
        # docstring; correctness is the DRAM comparison below.
        if hword != 0:
            raise SystemExit(
                "case %s halted with a0 = %d; the model reads "
                "CTL_CYCLE as 0, so a0 must be 0 there" % (name, hword)
            )
        if cause != 1:
            raise SystemExit("case %s halted with cause %d, not ECALL" % (name, cause))

        # `simd%02d` is what rv_simd_tb.v opens; the dsp -> simd rename missed
        # this prefix, so the bench read an empty imem and said NORETIRE.
        d = outdir / ("simd%02d" % n)
        d.mkdir(exist_ok=True)
        (d / "prog.hex").write_text(to_hex(words))
        (d / "dram.hex").write_text(to_hex([0] * DRAM_WORDS))
        (d / "dfin.hex").write_text(to_hex(dfin))
        meta = [cause, retired, _sum(dfin), kern_instr, len(words), ck, 0, 0]
        (d / "meta.hex").write_text(to_hex(meta))
        (d / "name.txt").write_text(name + "\n")
        index.append((n, name, len(words), retired, kern_instr, ck, note))
        print(
            "  simd%02d %-14s %5d words %8d retired %8d kernel instr  ck %08x"
            % (n, name, len(words), retired, kern_instr, ck)
        )

    # Bench-driven cases: only the image, because what they read arrives from
    # outside while the program is stopped and the model cannot predict it.
    for n, (name, fn) in enumerate(K.IX_CASES):
        K.SIMD = simd
        src = zero_regs() + fn()
        words, _ = assemble(src, base=0, symbols=ALL_SYMS)
        d = outdir / ("ix%02d" % n)
        d.mkdir(exist_ok=True)
        (d / "prog.hex").write_text(to_hex(words))
        (d / "name.txt").write_text(name + "\n")
        print("  ix%02d  %-14s %5d words  (bench-driven)" % (n, name, len(words)))

    (outdir / "ndsp.hex").write_text("%08x\n" % len(index))
    (outdir / "index.txt").write_text(
        "".join("%2d\t%s\t%d\t%d\t%d\t%08x\t%s\n" % r for r in index)
    )
    print("  %d DSP workload cases into %s" % (len(index), outdir))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--simd", type=int, default=8, choices=(2, 4, 8))
    ap.add_argument("--out", default=str(ROOT / "tests" / "pe" / "build" / "simd"))
    a = ap.parse_args()
    build(a.out, a.simd)


if __name__ == "__main__":
    main()
