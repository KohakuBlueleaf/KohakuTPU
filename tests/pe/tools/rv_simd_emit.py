"""Render the field table into the RTL decode header and the C intrinsics.

    python tests/pe/tools/rv_simd_emit.py           # write them
    python tests/pe/tools/rv_simd_emit.py --check   # fail if they have drifted

Two of the field table's four consumers are generated files, which is what makes
"one source of truth" enforceable rather than aspirational: `--check` is a
regeneration and a comparison, so a hand edit to either file fails a test
instead of silently disagreeing with the assembler.

The C side takes the shape 09B S2.2 argues for -- `.insn` strings with the
vector register number as a literal and every scalar operand left to GCC through
an `"r"` constraint, so the register allocator still allocates and there is no
compiler fork.
"""

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import rv_simd_isa as I
import rv_simd_isa_f as IF

ROOT = pathlib.Path(__file__).resolve().parents[3]
GEN = ROOT / "src" / "kohakuaccel" / "pe" / "rv32" / "simd" / "generated"
VH = GEN / "khs_isa.vh"
CH = GEN / "khs_intrin.h"

BANNER = (
    "GENERATED from tests/pe/tools/rv_simd_isa.py -- DO NOT EDIT.\n"
    "Regenerate with `python tests/pe/tools/rv_simd_emit.py`; the ISA test\n"
    "regenerates and compares, so a hand edit here fails rather than\n"
    "quietly disagreeing with the assembler and the golden model."
)

#: Keyed by (opcode, funct3), because custom-0 and custom-1 both number their
#: groups from zero and a bare funct3 would collide between the two tiers.
GROUP = {
    (I.OPC_KHD, I.F3_VINT): "INT",
    (I.OPC_KHD, I.F3_VBIT): "BIT",
    (I.OPC_KHD, I.F3_VSHI): "SH",
    (I.OPC_KHD, I.F3_VMAC): "MAC",
    (I.OPC_KHD, I.F3_VMOV): "MOV",
    (I.OPC_KHD, I.F3_VPRM): "PRM",
    (I.OPC_KHF, IF.F3_FMAC): "FMAC",
    (I.OPC_KHF, IF.F3_FRED): "FRED",
    (I.OPC_KHF, IF.F3_FCVT): "FCVT",
    (I.OPC_KHF, IF.F3_FALU): "FALU",
    (I.OPC_KHF, IF.F3_FSFU): "FSFU",
}

#: How each group packs its funct7. The permute group spends three bits on a
#: lane index because `vsldw` needs one and an R-type has no field left.
SHIFT = {
    (I.OPC_KHD, I.F3_VINT): 2,
    (I.OPC_KHD, I.F3_VSHI): 2,
    (I.OPC_KHD, I.F3_VMAC): 2,
    (I.OPC_KHD, I.F3_VPRM): 3,
    (I.OPC_KHD, I.F3_VBIT): 0,
    (I.OPC_KHD, I.F3_VMOV): 0,
    (I.OPC_KHF, IF.F3_FMAC): 2,
    (I.OPC_KHF, IF.F3_FRED): 2,
    (I.OPC_KHF, IF.F3_FCVT): 2,
    (I.OPC_KHF, IF.F3_FALU): 2,
    (I.OPC_KHF, IF.F3_FSFU): 2,
}

#: The identifier prefix each opcode major's constants carry.
PREFIX = {I.OPC_KHD: "KHS", I.OPC_KHF: "KHF"}


def _ident(name):
    """A stable Verilog/C identifier fragment for an instruction's operation."""
    base = name.removeprefix("v")
    if base.startswith("sldw"):
        base = "sldw"
    if base.split(".")[0] in ("pack", "unpkl", "unpkh"):
        return base.replace(".", "_").upper()
    # `fcvt` names its direction in the SECOND field -- f2i, i2f, f2f -- so the
    # stem alone would collide all three onto one identifier.
    if base.startswith("fcvt.") and base.count(".") >= 2:
        return "FCVT_" + base.split(".")[1].upper()
    return base.split(".")[0].upper()


def ops_by_group():
    """{(opcode, funct3): {op value: identifier}}, derived from the table alone."""
    out = {}
    for name, op in I.ISA.items():
        if op.funct7 is None:
            continue
        key = (op.opcode, op.group)
        val = op.funct7 >> SHIFT[key]
        out.setdefault(key, {}).setdefault(val, _ident(name))
    return out


def verilog():
    L = ["// " + ln for ln in BANNER.splitlines()]
    L += [
        "//",
        "// The decode is STRUCTURED, not a flat case: funct3 names the group,",
        "// funct7[1:0] is the element type for every typed group, and the",
        "// datapath reads that pair straight off the instruction word. A flat",
        "// 10-bit case would put the element width behind a decoder on the",
        "// operand path, which is where this core can least afford one.",
        "",
        "localparam [6:0] KHS_OPCODE = 7'h%02x;   // RISC-V custom-0" % I.OPC_KHD,
        "localparam [6:0] KHF_OPCODE = 7'h%02x;   // custom-1, reserved to the float tiers"
        % I.OPC_KHF,
        "",
        "// funct3: the group",
    ]
    for i, g in enumerate(I.groups()):
        L.append("localparam [2:0] KHS_F3_%-5s = 3'd%d;" % (g, i))
    L += ["", "// funct7[1:0]: the element type of a typed group"]
    for v, n in ((I.ET_S8, "S8"), (I.ET_S16, "S16"), (I.ET_S32, "S32")):
        L.append(
            "localparam [1:0] KHS_ET_%-3s = 2'd%d;   // %d-bit elements"
            % (n, v, I.ET_BITS[v])
        )

    L += ["", "// funct3: the float tier's groups, on custom-1"]
    for nm, v in (
        ("FMAC", IF.F3_FMAC),
        ("FRED", IF.F3_FRED),
        ("FCVT", IF.F3_FCVT),
        ("FALU", IF.F3_FALU),
        ("FSFU", IF.F3_FSFU),
    ):
        L.append("localparam [2:0] KHF_F3_%-5s = 3'd%d;" % (nm, v))
    L += ["", "// funct7[1:0]: the float element type"]
    for v, n in ((IF.FT_F16, "F16"), (IF.FT_F32, "F32")):
        L.append("localparam [1:0] KHF_FT_%-3s = 2'd%d;" % (n, v))

    L += [
        "",
        "// vec_alu's own opcodes, forwarded by khs_float_lane. A FALU or FSFU",
        "// instruction maps onto exactly one of these.",
    ]
    for n, v in sorted(IF.VEC_OP.items(), key=lambda kv: kv[1]):
        L.append("localparam [4:0] KHS_FOP_%-6s = 5'd%d;" % (n, v))

    for key, ops in sorted(ops_by_group().items()):
        opc, g = key
        sh = SHIFT[key]
        w = 7 - sh
        pfx = PREFIX[opc]
        gname = I.groups()[g] if opc == I.OPC_KHD else GROUP[key]
        L += [
            "",
            "// %s funct3 = %s_F3_%s: funct7[6:%d] is the operation%s"
            % (
                "custom-0" if opc == I.OPC_KHD else "custom-1",
                pfx,
                gname,
                sh,
                (
                    ", funct7[%d:0] the lane index" % (sh - 1)
                    if key == (I.OPC_KHD, I.F3_VPRM)
                    else ""
                ),
            ),
        ]
        for val, ident in sorted(ops.items()):
            L.append(
                "localparam [%d:0] %s_%s_%-9s = %d'd%d;"
                % (w - 1, pfx, GROUP[key], ident, w, val)
            )

    nkhd = sum(1 for o in I.ISA.values() if o.opcode == I.OPC_KHD)
    L += [
        "",
        "// %d instructions: %d integer on custom-0, %d float on custom-1."
        % (len(I.ISA), nkhd, len(I.ISA) - nkhd),
        "",
    ]
    return "\n".join(L) + "\n"


def _cat(parts):
    """A C string concatenation from ('lit', text) and ('arg', macro param).

    `.insn` needs the register FIELD spelled as a register name, so a vector
    index becomes the literal text `x` followed by the stringified macro
    argument: `x" #vd "`. Getting that prefix wrong assembles `3` where `x3` was
    meant, which is a different operand entirely.
    """
    out, buf = [], ""
    for kind, v in parts:
        if kind == "lit":
            buf += v
        else:
            if buf:
                out.append('"%s"' % buf)
                buf = ""
            out.append("#" + v)
    if buf:
        out.append('"%s"' % buf)
    return " ".join(out)


def _asm_line(op):
    """(template, output list, input list, is_memory) for one instruction."""
    if op.funct7 is None:
        vec = op.operands[0]
        parts = [
            ("lit", ".insn i 0x%02x, %d, x" % (op.opcode, op.group)),
            ("arg", vec.name),
            ("lit", ", %0, "),
            ("arg", "imm"),
        ]
        return _cat(parts), [], ['"r"(p)'], True

    slots, outs, ins = {}, [], []
    for o in op.operands:
        if o.kind == "xreg":
            slots[o.field] = [("lit", "%%%d" % (len(outs) + len(ins)))]
            (outs if o.name == "xd" else ins).append(o)
        else:
            slots[o.field] = [("lit", "x"), ("arg", o.name)]

    parts = [("lit", ".insn r 0x%02x, %d, 0x%02x, " % (op.opcode, op.group, op.funct7))]
    for i, field in enumerate(("rd", "rs1", "rs2")):
        if i:
            parts.append(("lit", ", "))
        parts += slots.get(field, [("lit", "x0")])
    return (
        _cat(parts),
        ['"=r"(_khs_r)' for _ in outs],
        ['"r"(%s)' % o.name for o in ins],
        False,
    )


def _asm_stmt(tmpl, outs, ins, clob, indent):
    """`__asm__ volatile(...)`, with only the sections that carry something.

    An empty section may be dropped only from the RIGHT, so a memory clobber
    with no operands still needs both colons in front of it.
    """
    tail = ""
    if clob:
        tail = " : %s : %s : %s" % (", ".join(outs), ", ".join(ins), clob)
    elif ins:
        tail = " : %s : %s" % (", ".join(outs), ", ".join(ins))
    elif outs:
        tail = " : %s" % ", ".join(outs)
    return "%s__asm__ volatile(%s%s)" % (indent, tmpl, tail.replace(" :  : ", " : : "))


def c_header():
    L = ["/* " + BANNER.splitlines()[0]]
    L += [" * " + ln for ln in BANNER.splitlines()[1:]]
    L[-1] += " */"
    L += [
        "",
        "/* Every intrinsic is `volatile`, and that is not caution.",
        " *",
        " * Vector register numbers are IMMEDIATES here, not operands the",
        " * compiler allocates -- which is what buys `no compiler fork`. The",
        " * consequence is that GCC cannot see the vector state at all: two",
        " * identical vdot calls are not one value, they accumulate. So the",
        " * compiler may not reorder, hoist or common these, and it cannot",
        " * software-pipeline the vector datapath. On an in-order single-issue",
        " * core whose multi-cycle ops stall in the existing hazard unit that",
        " * costs little; it is the honest price of the no-fork path.",
        " */",
        "#ifndef KHS_INTRIN_H",
        "#define KHS_INTRIN_H",
        "",
        "#include <stdint.h>",
        "",
        "#define KHS_VSPAD_BASE 0x40000000u",
        "",
    ]
    for name, op in I.ISA.items():
        tmpl, outs, ins, is_mem = _asm_line(op)
        cname = "khs_" + name.replace(".", "_")
        if is_mem:
            args = [op.operands[0].name, "p", "imm"]
        else:
            # An output scalar is the macro's VALUE, never one of its arguments.
            args = [o.name for o in op.operands if o.name != "xd"]
        clob = '"memory"' if is_mem else ""
        L.append("/* %s */" % op.doc.replace("*/", "* /"))
        if outs:
            L += [
                "#define %s(%s) ({ \\" % (cname, ", ".join(args)),
                "    int32_t _khs_r; \\",
                _asm_stmt(tmpl, outs, ins, clob, "    ") + "; \\",
                "    _khs_r; })",
            ]
        else:
            L += [
                "#define %s(%s) \\" % (cname, ", ".join(args)),
                _asm_stmt(tmpl, outs, ins, clob, "    "),
            ]
        L.append("")
    L += ["#endif /* KHS_INTRIN_H */", ""]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--check",
        action="store_true",
        help="fail if the files on disk differ from the table",
    )
    a = ap.parse_args()

    want = {VH: verilog(), CH: c_header()}
    if a.check:
        bad = 0
        for p, text in want.items():
            got = p.read_text() if p.exists() else None
            if got != text:
                bad += 1
                print(
                    "  DRIFT %s %s"
                    % (
                        p.relative_to(ROOT),
                        "is missing" if got is None else "differs from the field table",
                    )
                )
        print(
            "  %s -- %d of %d generated files agree with the table"
            % ("FAIL" if bad else "PASS", len(want) - bad, len(want))
        )
        return 1 if bad else 0

    GEN.mkdir(parents=True, exist_ok=True)
    for p, text in want.items():
        p.write_text(text)
        print("  wrote %s (%d lines)" % (p.relative_to(ROOT), text.count("\n")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
