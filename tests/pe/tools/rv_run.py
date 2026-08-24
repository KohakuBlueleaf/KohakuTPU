"""Run the RV32 PE's benches, or one hand-written program, with one command.

    python tests/pe/tools/rv_run.py --gate 1     pipeline against the model
    python tests/pe/tools/rv_run.py --gate 2     memory frontend
    python tests/pe/tools/rv_run.py --gate 3     PE + router + MAG + AXI RAM
    python tests/pe/tools/rv_run.py --gate 4     2 then 4 PEs on one NoC/MAG

    python tests/pe/tools/rv_run.py hello.s      one program, on the gate-3 bench

The last form assembles the file, runs it through the golden model, and checks
the hardware against that model on the SAME bench the suite runs on.

A program must end in `ecall`; a0 is the halt word.  Programs are wrapped in a
register-zeroing prologue by default because the register file is a RAM with no
reset -- the model starts at zero and the hardware starts at X.  --no-prologue
turns that off.
"""

import argparse
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from rv_asm import AsmError, assemble, to_hex
from rv_gen import MASK32, SYMS, _sum, zero_regs
from rv_model import DRAM_BASE, Machine

ROOT = pathlib.Path(__file__).resolve().parents[3]
TOOLS = ROOT / "tests" / "pe" / "tools"
BUILD = ROOT / "tests" / "pe" / "build"
XSIM = ROOT / "scripts" / "py" / "xsim.py"

DRAM_WORDS = 4096  # what the bench's AXI RAM covers, in 32-bit words
SPAD_WORDS = 2048

# Which generator has to have run before each gate, and what its benches are
# called in scripts/py/xsim.py. Level 4 is two: the cases run at 2 and at 4.
GATES = {
    1: (["rv_core"], "rv_gen.py"),
    2: (["rv_front"], None),
    3: (["rv_sys"], "rv_sys_gen.py"),
    4: (["rv_mc1", "rv_mc2", "rv_mc4"], "rv_mc_gen.py"),
}


def dram_ramp(n=DRAM_WORDS):
    return [(0x1234_0000 + 7 * i) & MASK32 for i in range(n)]


def run(argv):
    print("  $ " + " ".join(str(a) for a in argv), flush=True)
    return subprocess.run(argv, cwd=ROOT, check=False).returncode


def write_user(src, arg, dram_init, prologue):
    """Assemble, model, and write the six files rv_sys_tb's RV_USER mode reads."""
    text = (zero_regs() + src) if prologue else src
    words, _ = assemble(text, base=0, symbols=SYMS)

    m = Machine(imem_words=4096, spad_words=SPAD_WORDS, arg=arg, coreid=0x11)
    m.imem[: len(words)] = words
    for i, v in enumerate(dram_init):
        if v:
            m.dram[DRAM_BASE + 4 * i] = v & MASK32
    trace, cause, hword = m.run()

    dfin = [m.dram.get(DRAM_BASE + 4 * i, 0) for i in range(DRAM_WORDS)]
    d = BUILD / "sys" / "user"
    d.mkdir(parents=True, exist_ok=True)
    (d / "prog.hex").write_text(to_hex(words))
    (d / "dram.hex").write_text(to_hex(dram_init))
    (d / "dfin.hex").write_text(to_hex(dfin))
    (d / "spad.hex").write_text(to_hex([0] * 64))
    meta = [cause, hword, len(trace), _sum(dfin), _sum(m.spad), arg, len(words), 0]
    (d / "meta.hex").write_text(to_hex(meta))

    print(
        "  assembled %d words; the model retires %d instructions and halts "
        "with cause %d, a0 = 0x%08x" % (len(words), len(trace), cause, hword)
    )
    return words


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("source", nargs="?", help="an RV32I .s file to run")
    ap.add_argument("--gate", type=int, choices=sorted(GATES))
    ap.add_argument(
        "--arg",
        type=lambda s: int(s, 0),
        default=0,
        help="the kick argument, readable at CTL_ARG",
    )
    ap.add_argument(
        "--dram",
        choices=("zero", "ramp"),
        default="ramp",
        help="what the AXI RAM holds before the program runs",
    )
    ap.add_argument("--no-prologue", action="store_true")
    ap.add_argument("--regfile", choices=("lutram", "bram"), default="lutram")
    ap.add_argument(
        "--no-fwd-x",
        action="store_true",
        help="stall instead of bypassing the distance-1 hazard",
    )
    ap.add_argument("--wall", type=float, default=600.0)
    ap.add_argument("--keep", action="store_true")
    # Straight through to xsim, so a frontier point can drive the benches with
    # the same knobs its synthesis was given.
    ap.add_argument("--define", "-d", action="append", default=[])
    a = ap.parse_args()

    if (a.source is None) == (a.gate is None):
        ap.error("name a .s file or pass --gate, not both and not neither")

    defines = []
    for d in a.define:
        defines += ["-d", d]
    if a.regfile == "bram":
        defines += ["-d", "RV_RF_BRAM"]
    if a.no_fwd_x:
        defines += ["-d", "RV_FWD_X=0"]

    if a.gate is not None:
        benches, gen = GATES[a.gate]
        if gen and run([sys.executable, str(TOOLS / gen)]):
            return 1
    else:
        src = pathlib.Path(a.source)
        if not src.is_file():
            print("  no such file: %s" % src)
            return 1
        try:
            write_user(
                src.read_text(),
                a.arg,
                dram_ramp() if a.dram == "ramp" else [0] * DRAM_WORDS,
                not a.no_prologue,
            )
        except AsmError as e:
            print("  assembly failed: %s" % e)
            return 1
        except RuntimeError as e:
            print("  the model never halted: %s" % e)
            print("  every program must end in `ecall`.")
            return 1
        benches = ["rv_sys"]
        defines += ["-d", "RV_USER"]

    rc = 0
    for bench in benches:
        cmd = [sys.executable, str(XSIM), bench, "--wall", str(a.wall)] + defines
        if a.keep:
            cmd.append("--keep")
        rc |= run(cmd)
    return rc


if __name__ == "__main__":
    sys.exit(main())
