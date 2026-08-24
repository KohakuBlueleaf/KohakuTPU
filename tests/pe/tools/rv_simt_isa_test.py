"""Prove the field table's consumers agree, bit for bit.

    python tests/pe/tools/rv_simt_isa_test.py

Four consumers: the assembler, the golden model, the generated RTL header, and
the disassembler. This checks every pairing that can be checked in Python, and
regenerates the header to catch a hand edit.
"""

import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import rv_simt_asm as A
import rv_simt_isa as G
from rv_asm import assemble

ROOT = pathlib.Path(__file__).resolve().parents[3]

fails = []
checks = 0


def chk(ok, what):
    global checks
    checks += 1
    if not ok:
        fails.append(what)
        print("  FAIL %s" % what)


def sample(op):
    """A legal operand value for each of an instruction's fields."""
    out = {}
    for o in op.operands:
        if o.kind == "sreg":
            out[o.name] = 3
        elif o.kind == "vreg":
            out[o.name] = 5
        elif o is G.LANE:
            out[o.name] = 2
        elif o is G.CIDX:
            out[o.name] = 7
        else:
            out[o.name] = 1
    return out


print("--- encode/decode round trip, every instruction ---")
for name, op in G.ISA.items():
    args = sample(op)
    word = G.encode(name, **args)
    got = G.decode(word)
    chk(
        got is not None and got[0] == name and got[1] == args,
        "%s round trips (got %r)" % (name, got),
    )

print("--- every encoding is distinct ---")
seen = {}
for name, op in G.ISA.items():
    key = (op.opcode, op.group, op.funct7)
    chk(key not in seen, "%s does not collide with %s" % (name, seen.get(key)))
    seen[key] = name

print("--- the assembler agrees with the table ---")
for name, op in G.ISA.items():
    args = sample(op)
    toks = []
    for o in op.operands:
        v = args[o.name]
        toks.append(
            "s%d" % v if o.kind == "sreg" else "x%d" % v if o.kind == "vreg" else str(v)
        )
    words, _ = assemble("%s %s" % (name, ", ".join(toks)), base=0)
    chk(
        len(words) == 1 and words[0] == G.encode(name, **args),
        "assembler encodes %s as the table does" % name,
    )

print("--- the disassembler names what the assembler wrote ---")
for name, op in G.ISA.items():
    word = G.encode(name, **sample(op))
    text = A.disasm(word)
    chk(
        text is not None and text.split()[0] == name,
        "disasm(%s) names it (got %r)" % (name, text),
    )

print("--- a scalar operand refuses an ABI alias ---")
try:
    assemble("sadd t0, s1, s2", base=0)
    chk(False, "sadd t0 is refused")
except Exception:
    chk(True, "sadd t0 is refused")

print("--- an out-of-range register is refused ---")
try:
    G.encode("bcast", vd=1, vs1=2, lane=G.LANES)
    chk(False, "a lane index at LANES is refused")
except Exception:
    chk(True, "a lane index at LANES is refused")

print("--- the generated RTL header has not drifted ---")
rc = subprocess.run(
    [
        sys.executable,
        str(ROOT / "tests" / "pe" / "tools" / "rv_simt_emit.py"),
        "--check",
    ],
    capture_output=True,
    text=True,
    check=False,
)
chk(rc.returncode == 0, "kht_isa.vh agrees with the field table")

print("=" * 40)
if fails:
    print("  FAIL -- %d checks, %d errors" % (checks, len(fails)))
else:
    print("  PASS -- %d checks, 0 errors" % checks)
print("=" * 40)
sys.exit(1 if fails else 0)
