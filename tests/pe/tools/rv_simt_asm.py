"""GPU mnemonics for `rv_asm`, and the disassembler, both from the field table.

Registers as they appear in source:

    x0-x31 / ABI names   the PER-THREAD file. Ordinary RV32I mnemonics.
    s0-s31               the SCALAR file, and ONLY in a GPU mnemonic's scalar
                         operand position.

`s0` is also the RV32I ABI name for `x8`, so a scalar operand accepts the
explicit `sN` spelling alone and refuses every ABI alias. Reading `sadd t0, ...`
as scalar 5 would be a wrong answer with no witness, which is the one class
worth an error message.

    import rv_simt_asm            # registers the handler on import
    from rv_asm import assemble
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import rv_simt_isa as G                                          # noqa: E402
from rv_asm import AsmError, register_extension, _reg, _imm     # noqa: E402


def _sreg(tok):
    t = tok.strip().lower()
    if t.startswith("s") and t[1:].isdigit():
        n = int(t[1:])
        if 0 <= n < G.SREGS:
            return n
        raise AsmError("scalar register %r is outside s0..s%d"
                       % (tok, G.SREGS - 1))
    raise AsmError("not a scalar register: %r -- write sN, never an ABI name"
                   % tok)


def _operand(op, tok, pc, syms):
    if op.kind == "sreg":
        return _sreg(tok)
    if op.kind == "vreg":
        return _reg(tok)
    v = _imm(tok, syms, pc)
    return v


def _gpu(mn, ops, pc, syms):
    """Claim `mn` if the field table defines it. Returns words, or None."""
    if mn == "smv":
        if len(ops) != 2:
            raise AsmError("smv takes sd, ss1")
        return [G.encode("saddi", sd=_sreg(ops[0]), ss1=_sreg(ops[1]), imm=0)]

    # ONE WORD, deliberately: rv_asm's label pass sizes an unknown mnemonic at
    # one word, so a multi-word pseudo here fails the size/encode cross-check
    # rather than assembling short. A wide constant is `rdctl` from the launch
    # payload, which is where a shader's base pointers come from anyway.
    if mn == "sli":
        if len(ops) != 2:
            raise AsmError("sli takes sd, imm")
        v = _imm(ops[1], syms, pc)
        if not -2048 <= v <= 2047:
            raise AsmError("sli %d does not fit 12 bits; build it with "
                           "saddi/sslli/sori, or read it with rdctl" % v)
        return [G.encode("saddi", sd=_sreg(ops[0]), ss1=0, imm=v)]

    if mn not in G.ISA:
        return None
    op = G.ISA[mn]
    if len(ops) != len(op.operands):
        raise AsmError("%s takes %s, got %d operand(s)"
                       % (mn, ", ".join(o.name for o in op.operands), len(ops)))
    args = {}
    for o, tok in zip(op.operands, ops):
        v = _operand(o, tok, pc, syms)
        # A branch names a label; the field holds the displacement.
        if o is G.IMM and mn in ("sbeqz", "sbnez"):
            v = v - pc
        args[o.name] = v
    return [G.encode(mn, **args)]


def disasm(word):
    """One readable line, or None if no GPU format claims the word.

    The fourth consumer of the field table. It is what a bench prints on a
    mismatch, so it is generated from the table rather than hand-kept.
    """
    got = G.decode(word)
    if got is None:
        return None
    name, fields = got
    op = G.ISA[name]
    parts = []
    for o in op.operands:
        v = fields[o.name]
        if o.kind == "sreg":
            parts.append("s%d" % v)
        elif o.kind == "vreg":
            parts.append("x%d" % v)
        else:
            parts.append("%d" % v)
    return "%-10s %s" % (name, ", ".join(parts))


register_extension(_gpu)
