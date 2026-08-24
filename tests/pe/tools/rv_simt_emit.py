"""Render the GPU field table into the RTL decode header.

    python tests/pe/tools/rv_simt_emit.py           # write it
    python tests/pe/tools/rv_simt_emit.py --check   # fail if it has drifted

`--check` is a regeneration and a comparison, so a hand edit to the header fails
a test instead of silently disagreeing with the assembler and the golden model.

The SIMD tier emits a C intrinsic header as its fourth consumer. This one does
not, and the reason is the source language rather than an omission: the SIMT PE's
programs are SPIR-V shaders through a frontend, not C through GCC, and a `.insn`
macro whose semantics are "for each active lane" has no meaning in a C
expression. The fourth consumer here is the disassembler in `rv_simt_asm.py`,
which is generated from the same table and is what a bench prints on a mismatch.
"""

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import rv_simt_isa as I                                          # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[3]
GEN = ROOT / "src" / "kohakuaccel" / "pe" / "rv32" / "simt" / "generated"
VH = GEN / "kht_isa.vh"

BANNER = ("GENERATED from tests/pe/tools/rv_simt_isa.py -- DO NOT EDIT.\n"
          "Regenerate with `python tests/pe/tools/rv_simt_emit.py`; the ISA test\n"
          "regenerates and compares, so a hand edit here fails rather than\n"
          "quietly disagreeing with the assembler and the golden model.")

GROUP = {I.F3_SALU: "SALU", I.F3_SMOV: "SMOV", I.F3_DIV: "DIV",
         I.F3_SUB: "SUB", I.F3_VMEM: "VMEM", I.F3_FLT: "FLT"}


def _ident(name):
    """A stable Verilog identifier fragment for an instruction's operation.

    No prefix heuristics. A `startswith("vl")` rule meant to catch the memory
    group also caught `vlaneid` and emitted it as `KHT_SUB_MEM`; the vmem group
    is skipped by group id below, so the rule was never needed and only had
    room to be wrong.
    """
    return name.upper()


def ops_by_group():
    """{funct3: {funct7: identifier}} for custom-2, derived from the table alone."""
    out = {}
    for name, op in I.ISA.items():
        if op.funct7 is None or op.opcode != I.OPC_KHG:
            continue
        # The vmem group packs op/scale/width into funct7, so it gets named
        # fields below rather than one constant per encoding.
        if op.group == I.F3_VMEM:
            continue
        out.setdefault(op.group, {})[op.funct7] = _ident(name)
    return out


def verilog():
    L = ["// " + ln for ln in BANNER.splitlines()]
    L += [
        "//",
        "// The decode is STRUCTURED, not a flat case: funct3 names the group and",
        "// the datapath reads the group and its operation straight off the",
        "// instruction word. custom-2 carries the R-type groups; custom-3 carries",
        "// the I-type ones, because an I-type layout has no funct7 and would",
        "// otherwise hold one instruction per group.",
        "",
        "localparam [6:0] KHT_OPCODE  = 7'h%02x;   // RISC-V custom-2, R-type groups"
        % I.OPC_KHG,
        "localparam [6:0] KHGI_OPCODE = 7'h%02x;   // custom-3, I-type groups"
        % I.OPC_KHGI,
        "",
        "// funct3 on custom-2: the group",
    ]
    for i, g in enumerate(I.groups()):
        L.append("localparam [2:0] KHT_F3_%-6s = 3'd%d;" % (g, i))

    L += ["", "// funct3 on custom-3: one instruction each"]
    for i, g in enumerate(I.igroups()):
        L.append("localparam [2:0] KHGI_F3_%-5s = 3'd%d;" % (g, i))

    for g, ops in sorted(ops_by_group().items()):
        L += ["", "// custom-2 funct3 = KHT_F3_%s: funct7 is the operation"
              % GROUP[g]]
        for val, ident in sorted(ops.items()):
            L.append("localparam [6:0] KHT_%s_%-9s = 7'd%d;"
                     % (GROUP[g], ident, val))

    L += [
        "",
        "// The vmem group packs three things into funct7, so the datapath slices",
        "// it rather than comparing against one constant per encoding:",
        "//     funct7 = op<<4 | scale<<2 | width",
        "// op 0-2 take a per-lane offset from vs2; op 3-5 are LANE-LINEAR and",
        "// take no vector operand at all, so their addresses are known at",
        "// decode and the request count is one by construction.",
        "localparam [2:0] KHT_MEM_OP_L    = 3'd0;   // load, sign-extended",
        "localparam [2:0] KHT_MEM_OP_LU   = 3'd1;   // load, zero-extended",
        "localparam [2:0] KHT_MEM_OP_S    = 3'd2;   // store",
        "localparam [2:0] KHT_MEM_OP_LIN  = 3'd3;   // lane-linear load, signed",
        "localparam [2:0] KHT_MEM_OP_LINU = 3'd4;   // lane-linear load, unsigned",
        "localparam [2:0] KHT_MEM_OP_SIN  = 3'd5;   // lane-linear store",
        "localparam [1:0] KHT_MW_B = 2'd%d;" % I.MW_B,
        "localparam [1:0] KHT_MW_H = 2'd%d;" % I.MW_H,
        "localparam [1:0] KHT_MW_W = 2'd%d;" % I.MW_W,
        "",
        "// Machine shape. The ENCODING always allows 32 registers and any lane",
        "// index; these are what a build actually carries, so a program that",
        "// names more FAULTS rather than aliasing.",
        "localparam integer KHT_LANES = %d;" % I.LANES,
        "localparam integer KHT_WAVES = %d;" % I.WAVES,
        "localparam integer KHT_SREGS = %d;" % I.SREGS,
        "// A split pushes TWO entries and a join pops ONE, so a depth of D",
        "// permits D/2 nested divergent levels. Overflow is a fault.",
        "localparam integer KHT_IPDOM_DEPTH = %d;" % I.IPDOM_DEPTH,
        "",
    ]

    nkhg = sum(1 for o in I.ISA.values() if o.opcode == I.OPC_KHG)
    L += ["// %d instructions: %d on custom-2, %d on custom-3."
          % (len(I.ISA), nkhg, len(I.ISA) - nkhg), ""]
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="fail if the file on disk differs from the table")
    a = ap.parse_args()

    want = {VH: verilog()}
    if a.check:
        bad = 0
        for p, text in want.items():
            got = p.read_text() if p.exists() else None
            if got != text:
                bad += 1
                print("  DRIFT %s %s" % (p.relative_to(ROOT),
                                         "is missing" if got is None
                                         else "differs from the field table"))
        print("  %s -- %d of %d generated files agree with the table"
              % ("FAIL" if bad else "PASS", len(want) - bad, len(want)))
        return 1 if bad else 0

    GEN.mkdir(parents=True, exist_ok=True)
    for p, text in want.items():
        p.write_text(text)
        print("  wrote %s (%d lines)" % (p.relative_to(ROOT), text.count("\n")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
