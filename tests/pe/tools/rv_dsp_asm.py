"""Assembler support for the KohakuDSP instructions.

Importing this module registers a handler with `rv_asm`, so any program the
existing tooling assembles can use vector instructions with no other change:

    import rv_dsp_asm            # noqa: F401  -- registers the handler
    words, syms = assemble(src, base=0, symbols=SYMS)

Syntax follows the base assembler's, with two register namespaces added:

```
    vld    v3, 64(a0)          vector registers are v0..v7
    vst    v3, 0(a1)           accumulators are acc0..acc1
    vadd.s16 v0, v1, v2
    vdot.s8  acc0, v1, v2
    vslli.s32 v0, v1, 3
    vsplat v0, t1              t1 is an ordinary scalar register
    vextr  t0, v1, 2
    vsldw  v0, v1, v2, 3       assembles as the vsldw3 encoding
```

**Accumulators are `acc0`, not `a0`.** `a0` is an ABI scalar register name, and
an assembler that quietly took it for an accumulator would put the dot product
in the wrong file and report nothing.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import rv_asm                                                   # noqa: E402
from rv_asm import AsmError, _reg, _memop, _imm                 # noqa: E402
import rv_dsp_isa as I                                          # noqa: E402


def _vreg(tok):
    t = tok.strip().lower()
    if t.startswith("v") and t[1:].isdigit():
        n = int(t[1:])
        if 0 <= n < 32:
            return n
    raise AsmError("not a vector register: %r (use v0..v%d)" % (tok, I.VREGS - 1))


def _areg(tok):
    t = tok.strip().lower()
    if t.startswith("acc") and t[3:].isdigit():
        n = int(t[3:])
        if 0 <= n < 32:
            return n
    raise AsmError("not an accumulator: %r (use acc0..acc%d)" % (tok, I.NACC - 1))


_PARSE = {"vreg": _vreg, "areg": _areg, "xreg": _reg}


def _handle(mn, ops, pc, syms):
    """rv_asm extension hook: one word, or None if this is not a DSP mnemonic."""
    name = mn
    extra = {}

    # `vsldw vd, vs1, vs2, k` is the readable spelling of the vsldw<k> encoding;
    # the lane index is in funct7, so it must be a literal rather than a field.
    if mn == "vsldw":
        if len(ops) != 4:
            raise AsmError("vsldw takes vd, vs1, vs2, lane")
        k = _imm(ops[3], syms)
        if not 0 <= k < 8:
            raise AsmError("vsldw lane %d is outside 0..7" % k)
        name, ops = "vsldw%d" % k, ops[:3]

    op = I.ISA.get(name)
    if op is None:
        return None

    mem = op.funct7 is None
    want = 2 if mem else len(op.operands)
    if len(ops) != want:
        raise AsmError("%s takes %d operands, got %d" % (mn, want, len(ops)))

    vals = dict(extra)
    if mem:
        # `mn vX, off(reg)` -- one operand string carries both imm and xs1.
        reg_op = op.operands[0]
        vals[reg_op.name] = _PARSE[reg_op.kind](ops[0])
        off, base = _memop(ops[1], syms)
        vals["imm"] = off
        vals["xs1"] = base
    else:
        for o, tok in zip(op.operands, ops):
            vals[o.name] = (_imm(tok, syms) if o.kind == "imm"
                            else _PARSE[o.kind](tok))
    return [I.encode(name, **vals)]


rv_asm.register_extension(_handle)


def disasm(word):
    """One readable line for a DSP instruction, or None."""
    d = I.decode(word)
    if d is None:
        return None
    name, o = d
    sig = {"vreg": "v%d", "areg": "acc%d", "xreg": "x%d", "imm": "%d"}
    op = I.ISA[name]
    if op.funct7 is None:
        first = op.operands[0]
        return "%s %s, %d(x%d)" % (name, sig[first.kind] % o[first.name],
                                   o["imm"], o["xs1"])
    return "%s %s" % (name, ", ".join(sig[x.kind] % o[x.name]
                                      for x in op.operands))
