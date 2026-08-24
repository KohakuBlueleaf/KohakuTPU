"""Prove the field table's four consumers agree, bit for bit.

    python tests/pe/tools/rv_simd_isa_test.py

The four are the Python assembler, the golden model, the generated RTL decode
header and the generated C intrinsic header. "One source of truth" means
nothing unless something checks it, and the thing that checks it is this file --
`compiler/tests/test_ktpu_isa.py` applies the same discipline to the cluster and
vector ISAs, which is where the shape comes from.

What each check would catch:

| check | the defect it catches |
|---|---|
| assembler vs table | a mnemonic whose operand order differs from the encoding |
| encode/decode round trip | a field too narrow for the value it must hold |
| model executes every encoding | an instruction the table defines and the model forgot |
| generated files vs table | a hand edit, or a generator that has drifted |
| C `.insn` arguments vs table | the header naming a different opcode, group or funct7 |
| C operand order vs table | `rd`/`rs1`/`rs2` swapped -- the same instruction, wrong operands |
"""

import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import rv_simd_asm  # noqa: F401
import rv_simd_emit as E
import rv_simd_isa as I
from rv_asm import assemble
from rv_model import Halt
from rv_simd_model import DspMachine

FAIL = []


def check(cond, what):
    if not cond:
        FAIL.append(what)
        print("  FAIL %s" % what)


def _sample(op):
    """One legal operand set per instruction: distinct values, so a swap shows."""
    pick = {"vreg": [1, 2, 3], "areg": [0, 1], "xreg": [5, 6], "imm": [4, 3]}
    used = {}
    out = {}
    for o in op.operands:
        k = o.kind
        used[k] = used.get(k, 0)
        out[o.name] = pick[k][used[k] % len(pick[k])]
        used[k] += 1
    return out


def _asm_text(name, op, vals):
    sig = {"vreg": "v%d", "areg": "acc%d", "xreg": "x%d", "imm": "%d"}
    if op.funct7 is None:
        first = op.operands[0]
        return "%s %s, %d(x%d)" % (
            name,
            sig[first.kind] % vals[first.name],
            vals["imm"],
            vals["xs1"],
        )
    return "%s %s" % (name, ", ".join(sig[o.kind] % vals[o.name] for o in op.operands))


def main():
    print("--- %d instructions, four consumers ---" % len(I.ISA))

    # 1. the assembler encodes what the table says, and 2. it round-trips
    for name, op in I.ISA.items():
        vals = _sample(op)
        want = I.encode(name, **vals)
        words, _ = assemble("    " + _asm_text(name, op, vals), base=0)
        check(
            len(words) == 1 and words[0] == want,
            "assembler: %s gave %s, table says %08x"
            % (_asm_text(name, op, vals), " ".join("%08x" % w for w in words), want),
        )
        got = I.decode(want)
        check(
            got is not None and got[0] == name and got[1] == vals,
            "round trip: %s encoded %08x and decoded as %s" % (name, want, got),
        )

    # 3. the model has an implementation for every encoding the table defines
    m = DspMachine(simd=8, imem_words=64, spad_words=64, vspad_entries=8)
    m.x = [7] * 32
    m.x[0] = 0
    for name, op in I.ISA.items():
        vals = _sample(op)
        if op.funct7 is None:
            m.x[vals["xs1"]] = 0x4000_0000  # a legal vector scratchpad row
            vals["imm"] = 0
        word = I.encode(name, **vals)
        try:
            m.custom(op.opcode, word, 0)
        except Halt:
            FAIL.append("model: %s faulted on a legal encoding" % name)
            print("  FAIL model: %s faulted on a legal encoding" % name)
        except Exception as exc:
            FAIL.append("model: %s raised %r" % (name, exc))
            print("  FAIL model: %s raised %r" % (name, exc))
    check(
        len(m.vcount) == len(I.ISA),
        "model: counted %d distinct instructions, the table has %d"
        % (len(m.vcount), len(I.ISA)),
    )

    # 4. the generated files still match the table
    rc = subprocess.run(
        [sys.executable, str(HERE / "rv_simd_emit.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    check(rc.returncode == 0, "generated files: %s" % rc.stdout.strip())

    # 5. the generated Verilog's operation constants are the table's
    vh = E.VH.read_text()
    for key, ops in E.ops_by_group().items():
        pfx = E.PREFIX[key[0]]
        for val, ident in ops.items():
            pat = r"%s_%s_%s\s*=\s*\d+'d(\d+)\s*;" % (pfx, E.GROUP[key], ident)
            m2 = re.search(pat, vh)
            check(
                m2 is not None and int(m2.group(1)) == val,
                "khs_isa.vh: %s_%s_%s should be %d" % (pfx, E.GROUP[key], ident, val),
            )

    # 6. every C macro names the same opcode, group, funct7 and operand order
    ch = E.CH.read_text()
    RT = re.compile(r"^#define khs_(\S+?)\((.*?)\)", re.MULTILINE)
    INSN_R = re.compile(
        r'\.insn r 0x([0-9a-f]+), (\d+), 0x([0-9a-f]+), (.*?)"?\s*(?::|\)|$)'
    )
    INSN_I = re.compile(r"\.insn i 0x([0-9a-f]+), (\d+), x")
    seen = set()
    for m3 in RT.finditer(ch):
        cname = m3.group(1)
        name = next((n for n in I.ISA if n.replace(".", "_") == cname), None)
        check(name is not None, "khs_intrin.h: khs_%s is not in the table" % cname)
        if name is None:
            continue
        seen.add(name)
        op = I.ISA[name]
        body = ch[m3.end() : ch.index("\n\n", m3.end())]
        if op.funct7 is None:
            mi = INSN_I.search(body)
            check(
                mi is not None
                and int(mi.group(1), 16) == op.opcode
                and int(mi.group(2)) == op.group,
                "khs_intrin.h: %s has the wrong opcode or group" % name,
            )
            continue
        mr = INSN_R.search(body)
        check(mr is not None, "khs_intrin.h: %s has no `.insn r`" % name)
        if mr is None:
            continue
        check(
            int(mr.group(1), 16) == op.opcode
            and int(mr.group(2)) == op.group
            and int(mr.group(3), 16) == op.funct7,
            "khs_intrin.h: %s encodes 0x%s/%s/0x%s, table says 0x%02x/%d/0x%02x"
            % (
                name,
                mr.group(1),
                mr.group(2),
                mr.group(3),
                op.opcode,
                op.group,
                op.funct7,
            ),
        )
        # operand order: the rd / rs1 / rs2 slots in the template, in order
        slots = [s.strip() for s in mr.group(4).split(",")]
        want = []
        for field in ("rd", "rs1", "rs2"):
            o = next((x for x in op.operands if x.field == field), None)
            want.append("x0" if o is None else ("%N" if o.kind == "xreg" else o.name))
        got = [
            (
                "%N"
                if s.startswith("%")
                else ("x0" if s == "x0" else re.sub(r'^x"\s*#|\s*"$', "", s))
            )
            for s in slots
        ]
        check(
            got == want,
            "khs_intrin.h: %s operand order is %s, table says %s" % (name, got, want),
        )
    check(seen == set(I.ISA), "khs_intrin.h: missing %s" % sorted(set(I.ISA) - seen))

    print("=" * 40)
    print(
        "  %s -- %d instructions, %d disagreements"
        % ("FAIL" if FAIL else "PASS", len(I.ISA), len(FAIL))
    )
    print("=" * 40)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
