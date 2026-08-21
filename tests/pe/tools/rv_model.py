"""A golden RV32I model, and the memory map the PE presents to software.

The RTL is co-simulated against this one instruction at a time: for every
instruction that commits, the bench compares PC, destination register and
written value against the trace this model emits.  A core that is wrong for one
spacing of one hazard is then reported on the instruction it happened to,
rather than five thousand cycles later as a wrong answer.

The model owes the RTL exact agreement on the things that are easy to get
subtly wrong and impossible to notice: shifts use only the low five bits of the
operand, comparisons split signed from unsigned, JALR clears bit 0 of the
target, and a write to x0 is discarded but still retires.

Regions mirror rv_mem's single decoder.  Only the top four address bits choose.
"""

MASK = 0xFFFFFFFF

IMEM_BASE = 0x0000_0000
SPAD_BASE = 0x1000_0000
CTL_BASE = 0x2000_0000
PEER_BASE = 0x3000_0000
DRAM_BASE = 0x8000_0000

CTL_STATUS = CTL_BASE + 0x00
CTL_FLUSH = CTL_BASE + 0x04
CTL_INVAL = CTL_BASE + 0x08
CTL_CAUSE = CTL_BASE + 0x0C
CTL_COREID = CTL_BASE + 0x10
CTL_ARG = CTL_BASE + 0x14
CTL_CYCLE = CTL_BASE + 0x18
CTL_INSTRET = CTL_BASE + 0x1C
CTL_WROUT = CTL_BASE + 0x20

R_SPAD, R_CTL, R_PEER, R_DRAM, R_BAD = 1, 2, 3, 4, 7

CAUSE_ECALL, CAUSE_EBREAK, CAUSE_FAULT = 1, 2, 3


def region(a):
    if a & 0x8000_0000:
        return R_DRAM
    return {1: R_SPAD, 2: R_CTL, 3: R_PEER}.get((a >> 28) & 7, R_BAD)


def sx(v, bits):
    v &= (1 << bits) - 1
    return v - (1 << bits) if v & (1 << (bits - 1)) else v


class Halt(Exception):
    def __init__(self, cause, word):
        super().__init__("halt cause %d word 0x%08x" % (cause, word))
        self.cause = cause
        self.word = word


class Machine:
    """One PE's architectural state plus the memory its software can see.

    `peer_log` records every push in order, which is what the multi-core benches
    check the doorbell protocol against: the protocol's whole claim is that
    program order is arrival order.
    """

    def __init__(self, imem_words=2048, spad_words=2048, arg=0, coreid=0):
        self.x = [0] * 32
        self.pc = 0
        self.imem = list(imem_words * [0])
        self.spad = [0] * spad_words
        self.dram = {}
        self.arg = arg
        self.coreid = coreid
        self.instret = 0
        self.peer_log = []
        self.halted = False
        self.cause = 0
        self.halt_word = 0

    # ---- memory ---------------------------------------------------------
    def region_of(self, a):
        """The one extension seam in the memory map: an extension adds regions
        by overriding this, and the base map is unchanged."""
        return region(a)

    def _word(self, a):
        r = self.region_of(a)
        i = (a & 0x0FFF_FFFF) >> 2
        if r == R_SPAD:
            return self.spad[i % len(self.spad)]
        if r == R_DRAM:
            return self.dram.get(a & ~3, 0)
        if r == R_CTL:
            return self._ctl(a & ~3)
        raise Halt(CAUSE_FAULT, self.pc)

    def _ctl(self, a):
        if a == CTL_STATUS:
            return 0
        if a == CTL_CAUSE:
            return self.cause
        if a == CTL_COREID:
            return self.coreid
        if a == CTL_ARG:
            return self.arg
        if a == CTL_INSTRET:
            return self.instret
        return 0

    def _store_word(self, a, val, be):
        r = self.region_of(a)
        base = a & ~3
        if r == R_SPAD:
            i = ((a & 0x0FFF_FFFF) >> 2) % len(self.spad)
            old = self.spad[i]
        elif r == R_DRAM:
            old = self.dram.get(base, 0)
        elif r == R_CTL:
            return
        elif r == R_PEER:
            self.peer_log.append((base, val & MASK, be))
            return
        else:
            raise Halt(CAUSE_FAULT, self.pc)
        new = 0
        for b in range(4):
            src = val if (be >> b) & 1 else old
            new |= ((src >> (8 * b)) & 0xFF) << (8 * b)
        if r == R_SPAD:
            self.spad[i] = new & MASK
        else:
            self.dram[base] = new & MASK

    def load(self, a, f3):
        if f3 in (1, 5) and (a & 1):
            raise Halt(CAUSE_FAULT, self.pc)
        if f3 == 2 and (a & 3):
            raise Halt(CAUSE_FAULT, self.pc)
        if self.region_of(a) in (R_BAD, R_PEER):
            raise Halt(CAUSE_FAULT, self.pc)
        w = self._word(a)
        if f3 == 0:
            return sx(w >> (8 * (a & 3)), 8) & MASK
        if f3 == 4:
            return (w >> (8 * (a & 3))) & 0xFF
        if f3 == 1:
            return sx(w >> (16 * ((a >> 1) & 1)), 16) & MASK
        if f3 == 5:
            return (w >> (16 * ((a >> 1) & 1))) & 0xFFFF
        return w & MASK

    def store(self, a, val, f3):
        if f3 == 1 and (a & 1):
            raise Halt(CAUSE_FAULT, self.pc)
        if f3 == 2 and (a & 3):
            raise Halt(CAUSE_FAULT, self.pc)
        if self.region_of(a) == R_BAD:
            raise Halt(CAUSE_FAULT, self.pc)
        if f3 == 0:
            self._store_word(a, (val & 0xFF) * 0x0101_0101, 1 << (a & 3))
        elif f3 == 1:
            self._store_word(a, (val & 0xFFFF) * 0x0001_0001,
                             0b1100 if (a & 2) else 0b0011)
        else:
            self._store_word(a, val, 0xF)

    # ---- execution ------------------------------------------------------
    def fetch(self):
        i = (self.pc >> 2)
        if i >= len(self.imem):
            raise Halt(CAUSE_FAULT, self.pc)
        return self.imem[i]

    def step(self):
        """Retire one instruction; return (pc, rd, value) as the RTL reports it."""
        pc = self.pc
        ins = self.fetch()
        opc = ins & 0x7F
        rd = (ins >> 7) & 0x1F
        f3 = (ins >> 12) & 7
        rs1 = (ins >> 15) & 0x1F
        rs2 = (ins >> 20) & 0x1F
        f7 = (ins >> 25) & 0x7F
        a = self.x[rs1]
        b = self.x[rs2]

        imm_i = sx(ins >> 20, 12)
        imm_s = sx(((ins >> 25) << 5) | ((ins >> 7) & 0x1F), 12)
        imm_b = sx((((ins >> 31) & 1) << 12) | (((ins >> 7) & 1) << 11) |
                   (((ins >> 25) & 0x3F) << 5) | (((ins >> 8) & 0xF) << 1), 13)
        imm_u = ins & 0xFFFF_F000
        imm_j = sx((((ins >> 31) & 1) << 20) | (((ins >> 12) & 0xFF) << 12) |
                   (((ins >> 20) & 1) << 11) | (((ins >> 21) & 0x3FF) << 1), 21)

        nxt = (pc + 4) & MASK
        val = None

        if opc == 0x37:                                   # LUI
            val = imm_u
        elif opc == 0x17:                                 # AUIPC
            val = (pc + imm_u) & MASK
        elif opc == 0x6F:                                 # JAL
            val = nxt
            nxt = (pc + imm_j) & MASK
        elif opc == 0x67:                                 # JALR
            if f3 != 0:
                raise Halt(CAUSE_FAULT, pc)
            val = nxt
            nxt = (a + imm_i) & MASK & ~1
        elif opc == 0x63:                                 # BRANCH
            if f3 in (2, 3):
                raise Halt(CAUSE_FAULT, pc)
            sa, sb = sx(a, 32), sx(b, 32)
            take = {0: sa == sb, 1: sa != sb, 4: sa < sb,
                    5: sa >= sb, 6: a < b, 7: a >= b}[f3]
            if take:
                nxt = (pc + imm_b) & MASK
        elif opc == 0x03:                                 # LOAD
            if f3 in (3, 6, 7):
                raise Halt(CAUSE_FAULT, pc)
            val = self.load((a + imm_i) & MASK, f3)
        elif opc == 0x23:                                 # STORE
            if f3 > 2:
                raise Halt(CAUSE_FAULT, pc)
            self.store((a + imm_s) & MASK, b, f3)
        elif opc in (0x13, 0x33):                         # OP-IMM / OP
            second = imm_i & MASK if opc == 0x13 else b
            sh = (imm_i & 0x1F) if opc == 0x13 else (b & 0x1F)
            if opc == 0x13 and f3 == 1 and f7 != 0x00:
                raise Halt(CAUSE_FAULT, pc)
            if opc == 0x13 and f3 == 5 and f7 not in (0x00, 0x20):
                raise Halt(CAUSE_FAULT, pc)
            if opc == 0x33 and f7 not in (0x00, 0x20):
                raise Halt(CAUSE_FAULT, pc)
            if opc == 0x33 and f7 == 0x20 and f3 not in (0, 5):
                raise Halt(CAUSE_FAULT, pc)
            if f3 == 0:
                val = (a - b) & MASK if (opc == 0x33 and f7 == 0x20) \
                    else (a + second) & MASK
            elif f3 == 1:
                val = (a << sh) & MASK
            elif f3 == 2:
                val = 1 if sx(a, 32) < sx(second, 32) else 0
            elif f3 == 3:
                val = 1 if a < second else 0
            elif f3 == 4:
                val = a ^ second
            elif f3 == 5:
                val = (sx(a, 32) >> sh) & MASK if f7 == 0x20 else (a >> sh)
            elif f3 == 6:
                val = a | second
            else:
                val = a & second
        elif opc == 0x0F:                                 # FENCE, a NOP here
            pass
        elif opc == 0x73:                                 # SYSTEM
            if (ins >> 21) or ((ins >> 7) & 0x1FFF):
                raise Halt(CAUSE_FAULT, pc)
            raise Halt(CAUSE_EBREAK if (ins >> 20) & 1 else CAUSE_ECALL,
                       self.x[10])
        else:
            val = self.custom(opc, ins, pc)

        if val is not None and rd != 0:
            self.x[rd] = val & MASK
        else:
            rd, val = 0, 0

        self.pc = nxt
        self.instret += 1
        return pc, rd, val & MASK

    def custom(self, opc, ins, pc):
        """The one extension seam: an opcode the base ISA does not define.

        RV32I has none, so this faults and the base model behaves exactly as it
        did before the hook existed. A subclass returns the value to write to
        `rd`, or None when the instruction writes no scalar register.
        """
        raise Halt(CAUSE_FAULT, pc)

    def run(self, limit=200000):
        """Retire until the program halts; return (trace, cause, halt word).

        The halting instruction RETIRES.  It is the one that raised the halt, so
        the core cannot squash it without losing what it has to report -- and
        the RTL's retirement probe pulses for it, with no destination written.
        """
        trace = []
        try:
            for _ in range(limit):
                trace.append(self.step())
            raise RuntimeError("model did not halt within %d instructions" % limit)
        except Halt as h:
            trace.append((self.pc, 0, 0))
            self.instret += 1
            self.halted = True
            self.cause = h.cause
            self.halt_word = h.word
            return trace, h.cause, h.word


def trace_hex(trace):
    """72 bits a line: pc, rd, value -- one $readmemh entry per retirement."""
    return "".join("%08x%02x%08x\n" % (p, r, v) for p, r, v in trace)
