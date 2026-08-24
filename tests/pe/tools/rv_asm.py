"""A dependency-free RV32I assembler.

There is no external RISC-V toolchain in this flow, and there is deliberately
not going to be one until the PE runs real programs through the real memory
path.  A gcc that produces a 40 KB ELF is not a better starting point than a
200-line assembler when the thing being debugged is a forwarding path: the
assembler is small enough to be obviously right, and every test can name the
exact encoding it means.

Supports the whole of RV32I plus the usual pseudo-instructions, labels, and
`.word` / `.space` / `.align` data.  It emits 32-bit words; the caller decides
whether they become a $readmemh file or a NoC burst.

    from rv_asm import assemble
    words, labels = assemble(source, base=0)
"""

import re

ABI = {
    "zero": 0,
    "ra": 1,
    "sp": 2,
    "gp": 3,
    "tp": 4,
    "t0": 5,
    "t1": 6,
    "t2": 7,
    "s0": 8,
    "fp": 8,
    "s1": 9,
    "a0": 10,
    "a1": 11,
    "a2": 12,
    "a3": 13,
    "a4": 14,
    "a5": 15,
    "a6": 16,
    "a7": 17,
    "s2": 18,
    "s3": 19,
    "s4": 20,
    "s5": 21,
    "s6": 22,
    "s7": 23,
    "s8": 24,
    "s9": 25,
    "s10": 26,
    "s11": 27,
    "t3": 28,
    "t4": 29,
    "t5": 30,
    "t6": 31,
}

R_OPS = {
    "add": (0b000, 0b0000000),
    "sub": (0b000, 0b0100000),
    "sll": (0b001, 0b0000000),
    "slt": (0b010, 0b0000000),
    "sltu": (0b011, 0b0000000),
    "xor": (0b100, 0b0000000),
    "srl": (0b101, 0b0000000),
    "sra": (0b101, 0b0100000),
    "or": (0b110, 0b0000000),
    "and": (0b111, 0b0000000),
    # RV32M, funct7 = 0000001, in the EXISTING register-register group -- no new
    # major and none of the custom opcode space, which is already fully spoken
    # for. div/rem (funct3 100..111) are deliberately absent: they stay illegal.
    "mul": (0b000, 0b0000001),
    "mulh": (0b001, 0b0000001),
    "mulhsu": (0b010, 0b0000001),
    "mulhu": (0b011, 0b0000001),
}
I_OPS = {
    "addi": 0b000,
    "slti": 0b010,
    "sltiu": 0b011,
    "xori": 0b100,
    "ori": 0b110,
    "andi": 0b111,
}
SH_OPS = {
    "slli": (0b001, 0b0000000),
    "srli": (0b101, 0b0000000),
    "srai": (0b101, 0b0100000),
}
L_OPS = {"lb": 0b000, "lh": 0b001, "lw": 0b010, "lbu": 0b100, "lhu": 0b101}
S_OPS = {"sb": 0b000, "sh": 0b001, "sw": 0b010}
B_OPS = {
    "beq": 0b000,
    "bne": 0b001,
    "blt": 0b100,
    "bge": 0b101,
    "bltu": 0b110,
    "bgeu": 0b111,
}


class AsmError(Exception):
    pass


#: Extension mnemonic handlers, tried in registration order after the base ISA.
#: A handler takes ``(mnemonic, operand strings, pc, symbols)`` and returns a
#: list of words, or None if it does not claim the mnemonic.
#:
#: A hook rather than an import, so this file stays dependency-free: the DSP
#: extension's table is built on the compiler package, and a base-ISA gate must
#: not acquire that dependency to keep running.
EXTENSIONS = []


def register_extension(fn):
    if fn not in EXTENSIONS:
        EXTENSIONS.append(fn)


def _reg(tok):
    t = tok.strip().lower()
    if t in ABI:
        return ABI[t]
    if t.startswith("x") and t[1:].isdigit():
        n = int(t[1:])
        if 0 <= n < 32:
            return n
    raise AsmError("not a register: %r" % tok)


def _fits(v, bits, signed=True):
    if signed:
        lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    else:
        lo, hi = 0, (1 << bits) - 1
    if not (lo <= v <= hi):
        raise AsmError("immediate %d does not fit %d signed bits" % (v, bits))
    return v & ((1 << bits) - 1)


def _r(op, f3, f7, rd, rs1, rs2):
    return (f7 << 25) | (rs2 << 20) | (rs1 << 15) | (f3 << 12) | (rd << 7) | op


def _i(op, f3, rd, rs1, imm):
    return (_fits(imm, 12) << 20) | (rs1 << 15) | (f3 << 12) | (rd << 7) | op


def _s(op, f3, rs1, rs2, imm):
    v = _fits(imm, 12)
    return (
        (((v >> 5) & 0x7F) << 25)
        | (rs2 << 20)
        | (rs1 << 15)
        | (f3 << 12)
        | ((v & 0x1F) << 7)
        | op
    )


def _b(op, f3, rs1, rs2, imm):
    if imm & 1:
        raise AsmError("branch offset %d is odd" % imm)
    v = _fits(imm, 13)
    return (
        (((v >> 12) & 1) << 31)
        | (((v >> 5) & 0x3F) << 25)
        | (rs2 << 20)
        | (rs1 << 15)
        | (f3 << 12)
        | (((v >> 1) & 0xF) << 8)
        | (((v >> 11) & 1) << 7)
        | op
    )


def _u(op, rd, imm):
    return ((imm & 0xFFFFF) << 12) | (rd << 7) | op


def _j(op, rd, imm):
    if imm & 1:
        raise AsmError("jump offset %d is odd" % imm)
    v = _fits(imm, 21)
    return (
        (((v >> 20) & 1) << 31)
        | (((v >> 1) & 0x3FF) << 21)
        | (((v >> 11) & 1) << 20)
        | (((v >> 12) & 0xFF) << 12)
        | (rd << 7)
        | op
    )


# Split an operand list, keeping `off(reg)` together.
def _split(rest):
    return [p.strip() for p in rest.split(",") if p.strip()]


_MEMOP = re.compile(r"^(-?[\w+\-]*)\(\s*([\w]+)\s*\)$")


def _memop(tok, syms):
    m = _MEMOP.match(tok.strip())
    if not m:
        raise AsmError("not an offset(reg) operand: %r" % tok)
    off = m.group(1).strip()
    return (_imm(off, syms) if off else 0), _reg(m.group(2))


def _imm(tok, syms, pc=None):
    t = tok.strip()
    if t in syms:
        return syms[t]
    # %hi / %lo let `la` be written out by hand where a test wants to see it
    if t.startswith("%hi(") and t.endswith(")"):
        v = _imm(t[4:-1], syms)
        return ((v + 0x800) >> 12) & 0xFFFFF
    if t.startswith("%lo(") and t.endswith(")"):
        v = _imm(t[4:-1], syms)
        return ((v & 0xFFF) ^ 0x800) - 0x800
    try:
        return int(t, 0)
    except ValueError:
        pass
    # a small expression grammar: sym+n, sym-n, n+n
    for sep in ("+", "-"):
        i = t.rfind(sep)
        if i > 0:
            a = _imm(t[:i], syms)
            b = _imm(t[i + 1 :], syms)
            return a + b if sep == "+" else a - b
    raise AsmError("unresolved symbol or bad immediate: %r" % tok)


# How many words each mnemonic emits, needed by the label pass before the
# operands can be evaluated.  Only the pseudo-instructions vary.
def _size(mn, ops):
    if mn == "li":
        return 2
    if mn == "la":
        return 2
    if mn == ".word":
        return len(ops)
    if mn == ".space":
        return 0  # resolved in pass 1, where the count is known
    return 1


def assemble(src, base=0, symbols=None):
    """Assemble `src`, returning (list of 32-bit words, label dict).

    `base` is the byte address the first word lands at; labels are absolute.
    `symbols` seeds the symbol table, which is how a test passes in a memory
    map without writing the constants twice.
    """
    syms = dict(symbols or {})
    lines = []
    for raw in src.splitlines():
        text = raw.split("#")[0].split("//")[0].strip()
        while text:
            m = re.match(r"^([.\w]+)\s*:\s*(.*)$", text)
            if m and not m.group(1).startswith("."):
                lines.append(("label", m.group(1), None))
                text = m.group(2).strip()
                continue
            parts = text.split(None, 1)
            lines.append(("op", parts[0].lower(), parts[1] if len(parts) > 1 else ""))
            text = ""

    # pass 1: addresses
    pc = base
    plan = []
    for kind, a, b in lines:
        if kind == "label":
            syms[a] = pc
            continue
        mn, rest = a, b
        if mn == ".space":
            n = int(_imm(rest, syms))
            plan.append((pc, mn, rest, n))
            pc += 4 * n
            continue
        if mn == ".align":
            n = int(_imm(rest, syms))
            pad = (-(pc // 4)) % n
            plan.append((pc, ".space", str(pad), pad))
            pc += 4 * pad
            continue
        if mn == "equ":
            name, val = _split(rest)
            syms[name] = _imm(val, syms)
            continue
        n = _size(mn, _split(rest))
        plan.append((pc, mn, rest, n))
        pc += 4 * n

    # pass 2: encode
    words = []
    for at, mn, rest, n in plan:
        words.extend(_encode(mn, rest, at, syms, n))
    if len(words) * 4 != pc - base:
        raise AsmError("size pass disagrees with encode pass")
    return words, syms


def _encode(mn, rest, pc, syms, n):
    ops = _split(rest)

    if mn == ".word":
        return [_imm(o, syms) & 0xFFFFFFFF for o in ops]
    if mn == ".space":
        return [0] * n

    # ---- pseudo-instructions -------------------------------------------
    if mn == "nop":
        return [_i(0b0010011, 0b000, 0, 0, 0)]
    if mn == "mv":
        return [_i(0b0010011, 0b000, _reg(ops[0]), _reg(ops[1]), 0)]
    if mn == "not":
        return [_i(0b0010011, 0b100, _reg(ops[0]), _reg(ops[1]), -1)]
    if mn == "neg":
        return [_r(0b0110011, 0b000, 0b0100000, _reg(ops[0]), 0, _reg(ops[1]))]
    if mn == "seqz":
        return [_i(0b0010011, 0b011, _reg(ops[0]), _reg(ops[1]), 1)]
    if mn == "snez":
        return [_r(0b0110011, 0b011, 0b0000000, _reg(ops[0]), 0, _reg(ops[1]))]
    if mn in ("li", "la"):
        rd = _reg(ops[0])
        v = _imm(ops[1], syms) & 0xFFFFFFFF
        hi = ((v + 0x800) >> 12) & 0xFFFFF
        lo = ((v & 0xFFF) ^ 0x800) - 0x800
        return [_u(0b0110111, rd, hi), _i(0b0010011, 0b000, rd, rd, lo)]
    if mn == "j":
        return [_j(0b1101111, 0, _imm(ops[0], syms) - pc)]
    if mn == "jal" and len(ops) == 1:
        return [_j(0b1101111, 1, _imm(ops[0], syms) - pc)]
    if mn == "jr":
        return [_i(0b1100111, 0b000, 0, _reg(ops[0]), 0)]
    if mn == "ret":
        return [_i(0b1100111, 0b000, 0, 1, 0)]
    if mn == "call":
        return [_j(0b1101111, 1, _imm(ops[0], syms) - pc)]
    if mn in ("beqz", "bnez", "bltz", "bgez", "bgtz", "blez"):
        rs = _reg(ops[0])
        off = _imm(ops[1], syms) - pc
        tbl = {
            "beqz": ("beq", rs, 0),
            "bnez": ("bne", rs, 0),
            "bltz": ("blt", rs, 0),
            "bgez": ("bge", rs, 0),
            "bgtz": ("blt", 0, rs),
            "blez": ("bge", 0, rs),
        }
        op, a, b = tbl[mn]
        return [_b(0b1100011, B_OPS[op], a, b, off)]
    if mn in ("bgt", "ble", "bgtu", "bleu"):
        a, b = _reg(ops[1]), _reg(ops[0])
        off = _imm(ops[2], syms) - pc
        real = {"bgt": "blt", "ble": "bge", "bgtu": "bltu", "bleu": "bgeu"}[mn]
        return [_b(0b1100011, B_OPS[real], a, b, off)]

    # ---- real instructions ----------------------------------------------
    if mn in R_OPS:
        f3, f7 = R_OPS[mn]
        return [_r(0b0110011, f3, f7, _reg(ops[0]), _reg(ops[1]), _reg(ops[2]))]
    if mn in I_OPS:
        return [
            _i(0b0010011, I_OPS[mn], _reg(ops[0]), _reg(ops[1]), _imm(ops[2], syms))
        ]
    if mn in SH_OPS:
        f3, f7 = SH_OPS[mn]
        sh = _imm(ops[2], syms)
        if not 0 <= sh < 32:
            raise AsmError("shift amount %d out of range" % sh)
        return [_r(0b0010011, f3, f7, _reg(ops[0]), _reg(ops[1]), sh)]
    if mn in L_OPS:
        off, rs1 = _memop(ops[1], syms)
        return [_i(0b0000011, L_OPS[mn], _reg(ops[0]), rs1, off)]
    if mn in S_OPS:
        off, rs1 = _memop(ops[1], syms)
        return [_s(0b0100011, S_OPS[mn], rs1, _reg(ops[0]), off)]
    if mn in B_OPS:
        return [
            _b(
                0b1100011,
                B_OPS[mn],
                _reg(ops[0]),
                _reg(ops[1]),
                _imm(ops[2], syms) - pc,
            )
        ]
    if mn == "lui":
        return [_u(0b0110111, _reg(ops[0]), _imm(ops[1], syms))]
    if mn == "auipc":
        return [_u(0b0010111, _reg(ops[0]), _imm(ops[1], syms))]
    if mn == "jal":
        return [_j(0b1101111, _reg(ops[0]), _imm(ops[1], syms) - pc)]
    if mn == "jalr":
        if len(ops) == 2:
            off, rs1 = _memop(ops[1], syms)
            return [_i(0b1100111, 0b000, _reg(ops[0]), rs1, off)]
        return [_i(0b1100111, 0b000, _reg(ops[0]), _reg(ops[1]), _imm(ops[2], syms))]
    if mn == "fence":
        return [0b0000000000000000000000000001111 | (0xFF << 20)]
    if mn == "ecall":
        return [0b1110011]
    if mn == "ebreak":
        return [(1 << 20) | 0b1110011]

    for ext in EXTENSIONS:
        got = ext(mn, ops, pc, syms)
        if got is not None:
            return got

    raise AsmError("unknown mnemonic %r" % mn)


def to_hex(words):
    """One 8-digit word per line, which is what $readmemh wants."""
    return "".join("%08x\n" % (w & 0xFFFFFFFF) for w in words)
