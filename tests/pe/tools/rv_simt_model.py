"""A golden SIMT model: W resident waves, L lanes, active masks, an IPDOM stack.

The RTL is co-simulated against this one instruction at a time, and the
comparison is per LANE: for every instruction that commits, the bench checks the
PC, the destination and the value in every lane. A core that keeps its
arithmetic and loses its masks is wrong here on the instruction it happened to.

It also counts what the coalescer must do, because "requests issued per gather"
is a witness and not a statistic -- see `Wave.pending` and `GpuMachine.reqs`.

## The register-class rule

RV32I opcode space addresses the PER-THREAD file, so `x5` is L values. The
scalar file `s0`-`s31` is per wave and is reached only through custom-2/3.
`ballot` and `redux` are the only vector-to-scalar path; there is no raw move,
because under a mask it would have to invent which lane wins.

## The IPDOM contract, stated exactly because the design note does not

`split` pushes TWO entries and `join` pops ONE:

    split vs1:  t = mask & pred ; f = mask & ~pred
                push(mask)          <- restores the outer mask (reconvergence)
                push(f)             <- the false path
                mask = t ; pc += 4
    join:       mask = pop() ; pc += 4

A popped entry carries no PC: the resume point is always the popping `join`'s
own `pc + 4`. That is what makes the entry L bits wide instead of L + 32, and it
is exact for structured control flow, which SPIR-V guarantees by naming a merge
block for every selection and loop.

Two consequences a frontend must honour:

* **Every `split` needs exactly two `join`s**, so an `if` with no `else` emits an
  empty else block. Balanced by construction beats balanced by analysis.
* **A depth bound of D permits D/2 nested divergent levels, not D-1.** Each
  split costs two entries. At the default D = 8 that is four levels; seven needs
  D = 16. Overflow is a FAULT -- never a wrap, never a mask merge.
"""

MASK = 0xFFFFFFFF

# G9's arithmetic is the SIMD tier's, so its MODEL is too: `khs_fp32.fpu`
# reproduces the built unit rather than the correctly-rounded definition, which
# is the only way a golden model can be bit-exact against `rv_fpu`.
import khs_fp32 as KF
import khs_seed_tab as KSEED

F32_ONE = 0x3F80_0000

# Regions, as the SIMT PE's MEM stage decodes them. LDS replaces the SIMD tier's
# vector scratchpad at the same address, because it is the same array.
SPAD_BASE = 0x1000_0000
CTL_BASE = 0x2000_0000
LDS_BASE = 0x4000_0000
DRAM_BASE = 0x8000_0000

R_SPAD, R_CTL, R_LDS, R_DRAM, R_BAD = 1, 2, 4, 8, 7

CAUSE_ECALL, CAUSE_EBREAK, CAUSE_FAULT = 1, 2, 3

OPC_KHG, OPC_KHGI = 0x5B, 0x7B

#: One 32-byte line is one memory entry, one flit and one L1 line, so it is also
#: the unit the coalescer groups by.
LINE_BYTES = 32

#: LAUNCH policy bits (compute-unit.md s2.4). The model's memory is coherent, so
#: only the two that change architectural state act here.
POL_I_INV, POL_D_INV, POL_D_WB, POL_LDS_CLEAR, POL_CNT_RESET = 1, 2, 4, 8, 16

#: LDS granules: 8 words, one row across the eight banks.
GRAN_WORDS = 8

F32_QNAN = 0x7FC0_0000
F16_QNAN = 0x7E00


def sx(v, bits):
    v &= (1 << bits) - 1
    return v - (1 << bits) if v & (1 << (bits - 1)) else v


def f32_is_nan(a):
    return (a & 0x7F80_0000) == 0x7F80_0000 and (a & 0x007F_FFFF) != 0


def f32_key(a):
    """An integer that orders binary32 values as reals; +0 and -0 tie."""
    mag = a & 0x7FFF_FFFF
    return -mag if a >> 31 else mag


def f32_minmax(a, b, want_max):
    """IEEE minNum/maxNum: a NaN loses to a number, two NaNs give the canonical
    quiet NaN, and -0 orders below +0 (RISC-V FMIN/FMAX)."""
    if f32_is_nan(a) and f32_is_nan(b):
        return F32_QNAN
    if f32_is_nan(a):
        return b
    if f32_is_nan(b):
        return a
    ka, kb = f32_key(a), f32_key(b)
    if ka == kb:
        # Equal as reals; only the zeros differ in bits, and the sign decides.
        neg = (a | b) >> 31 if not want_max else (a & b) >> 31
        return (neg << 31) | (a & 0x7FFF_FFFF)
    pick_a = (ka > kb) if want_max else (ka < kb)
    return a if pick_a else b


def f32_cmp(a, b, how):
    """vflt/vfle/vfeq: 1 or 0. Any NaN compares false; +0 == -0."""
    if f32_is_nan(a) or f32_is_nan(b):
        return 0
    ka, kb = f32_key(a), f32_key(b)
    return int({"lt": ka < kb, "le": ka <= kb, "eq": ka == kb}[how])


def f16_to_f32(h):
    """binary16 -> binary32, exact. A NaN keeps its payload and becomes quiet."""
    s, e, m = (h >> 15) & 1, (h >> 10) & 0x1F, h & 0x3FF
    if e == 0x1F:
        return (s << 31) | 0x7F80_0000 | ((0x40_0000 | (m << 13)) if m else 0)
    if e == 0:
        if m == 0:
            return s << 31
        # A binary16 subnormal is m x 2^-24; normalise it.
        sh = 0
        while not (m & 0x400):
            m <<= 1
            sh += 1
        return (s << 31) | ((127 - 15 + 1 - sh) << 23) | ((m & 0x3FF) << 13)
    return (s << 31) | ((e - 15 + 127) << 23) | (m << 13)


def f32_to_f16(a):
    """binary32 -> binary16, round to nearest even, overflow to infinity, a NaN to
    the signed canonical quiet NaN."""
    s, e, m = (a >> 31) & 1, (a >> 23) & 0xFF, a & 0x7F_FFFF
    if e == 0xFF:
        return (s << 15) | (F16_QNAN if m else 0x7C00)
    if e == 0:
        return s << 15  # a binary32 subnormal is far below binary16's range
    sig = m | 0x80_0000  # 24 bits, value sig x 2^(e - 150)
    # Units of the result's last place: 2^-24 for subnormals, 2^(E-10) else.
    ex = e - 127
    ulp = -24 if ex < -14 else ex - 10
    sh = ulp - (e - 150)  # bits of sig below the result's last place
    if sh >= 32:
        q = 0
    else:
        q, rem = sig >> sh, sig & ((1 << sh) - 1)
        half = 1 << (sh - 1)
        if rem > half or (rem == half and (q & 1)):
            q += 1
    if ex < -14:
        # q is the subnormal mantissa; 0x400 is exactly the smallest normal.
        return (s << 15) | q
    if q == 0x800:  # rounding carried into the next binade
        q >>= 1
        ex += 1
    if ex > 15:
        return (s << 15) | 0x7C00
    return (s << 15) | ((ex + 15) << 10) | (q & 0x3FF)


def region(a):
    if a & 0x8000_0000:
        return R_DRAM
    return {1: R_SPAD, 2: R_CTL, 4: R_LDS}.get((a >> 28) & 7, R_BAD)


class Halt(Exception):
    def __init__(self, cause, word):
        super().__init__("halt cause %d word 0x%08x" % (cause, word))
        self.cause = cause
        self.word = word


class Wave:
    """One resident wave: a PC, a mask, a per-lane register file, a scalar file."""

    def __init__(self, wid, lanes, pc=0, mask=None, depth=8):
        self.wid = wid
        self.lanes = lanes
        self.pc = pc
        self.mask = (1 << lanes) - 1 if mask is None else mask
        self.x = [[0] * lanes for _ in range(32)]
        self.s = [0] * 32
        self.stack = []
        self.depth = depth
        self.done = False
        self.hi_water = 0
        # Which workgroup this context runs, its index inside it, and the slot
        # (group of wave contexts and LDS share) it occupies.
        self.wg = 0
        self.wiw = wid
        self.slot = 0
        # None, ("bar",) or ("wait", counter, target): why the wave cannot issue.
        self.sleep = None

    def active(self):
        return [i for i in range(self.lanes) if (self.mask >> i) & 1]

    def push(self, m):
        if len(self.stack) >= self.depth:
            raise Halt(CAUSE_FAULT, self.pc)
        self.stack.append(m)
        self.hi_water = max(self.hi_water, len(self.stack))

    def pop(self):
        if not self.stack:
            raise Halt(CAUSE_FAULT, self.pc)
        return self.stack.pop()


def coalesce(addrs):
    """Leader/follower, over 32-byte lines. Returns the list of lines served.

    The forward-progress property the loop rests on: every pass serves at least
    its own leader, so it terminates in at most len(addrs) passes. The bench
    asserts that in simulation rather than trusting this comment.
    """
    todo = dict(enumerate(addrs))
    lines = []
    while todo:
        lead = min(todo)
        line = todo[lead] & ~(LINE_BYTES - 1)
        served = [i for i, a in todo.items() if (a & ~(LINE_BYTES - 1)) == line]
        assert lead in served, "a pass that does not serve its own leader"
        lines.append((line, served))
        for i in served:
            del todo[i]
    return lines


class GpuMachine:
    """The PE's architectural state, and the memory its shaders can see."""

    def __init__(
        self,
        lanes=8,
        waves=16,
        imem_words=2048,
        lds_words=8192,
        depth=8,
        ctl=None,
        nlive=1,
        has_fsfu=False,
        counters=4,
    ):
        self.lanes = lanes
        # THE SEEDS ARE A UNIT COUNT ON THE PE and 0 is the shipping value, so
        # the model faults on one unless the caller says the build carries them.
        self.has_fsfu = has_fsfu
        self.imem = [0] * imem_words
        self.lds = [0] * lds_words
        self.spad = [0] * 2048
        self.dram = {}
        self.ctl = list(ctl or [0] * 32)
        # `waves` is what the build CARRIES; `nlive` is what this kick launches,
        # clamped to it. The two are different questions and conflating them is
        # how "16 waves" ends up meaning storage in one place and issue in
        # another.
        self.waves = [Wave(i, lanes, depth=depth) for i in range(waves)]
        for w in self.waves[max(1, min(nlive, waves)) :]:
            w.done = True
        self.reqs = []
        self.instret = 0
        self.cause = 0
        self.halt_word = 0
        self.counters = [0] * counters
        # What the stream engine sent to other units, in issue order: the bench
        # compares these, since they never reach this core's memory.
        self.sent = []
        self.credits = []
        # A KICK is a one-workgroup launch of `nlive` waves (compute-unit.md s2.1).
        self.launch_id = 1
        self.wg_base, self.wg_count = 0, 1
        self.wg_waves = max(1, min(nlive, waves))
        self.wg_lds = 0
        self.wg_next = 1
        self.nslots = 1
        self.launch_pc = 0

    # ---- launches and workgroups (compute-unit.md s2) ---------------------
    def launch(self, pc, arg, wg_base=0, wg_count=1, wg_waves=1, wg_lds=0, policy=0):
        """Start a LAUNCH: the slice's workgroups run in as many slots as fit."""
        self.ctl[0] = arg & MASK
        self.launch_id = (self.launch_id + 1) & MASK
        self.launch_pc = pc & MASK
        self.wg_base = wg_base & 0xFFFF
        self.wg_count = (wg_count & 0xFFFF) or 1
        self.wg_waves = (wg_waves & 0x1F) or 1
        self.wg_lds = wg_lds & 0x7FF
        if self.wg_waves > len(self.waves):
            raise Halt(CAUSE_FAULT, pc)
        nslots = len(self.waves) // self.wg_waves
        if self.wg_lds:
            nslots = min(nslots, len(self.lds) // (self.wg_lds * GRAN_WORDS))
        if nslots == 0:
            raise Halt(CAUSE_FAULT, pc)
        self.nslots = nslots
        if policy & POL_LDS_CLEAR:
            self.lds = [0] * len(self.lds)
        if policy & POL_CNT_RESET:
            self.counters = [0] * len(self.counters)
        for w in self.waves:
            w.done = True
            w.sleep = None
        self.wg_next = 0
        self._fill_slots()

    def _fill_slots(self):
        """Start the next workgroup in every slot whose waves have all ended."""
        for s in range(self.nslots):
            if self.wg_next >= self.wg_count:
                return
            ws = self.waves[s * self.wg_waves : (s + 1) * self.wg_waves]
            if not all(w.done for w in ws):
                continue
            # Registers are NOT cleared: the hardware's register file keeps what
            # the slot's previous wave left, so a shader initialises what it reads.
            for j, w in enumerate(ws):
                w.pc = self.launch_pc
                w.mask = (1 << self.lanes) - 1
                w.stack = []
                w.done = False
                w.sleep = None
                w.wg = (self.wg_base + self.wg_next) & 0xFFFF
                w.wiw = j
                w.slot = s
            self.wg_next += 1

    def receive(self, buf_id, offset, words, count=True):
        """A CU_DATA burst from another unit landing in LDS (compute-unit.md s6.2)."""
        if len(words) % GRAN_WORDS:
            raise ValueError("a burst is whole 32-byte granules")
        if not (buf_id == 0 or 8 <= buf_id <= 11):
            raise ValueError("buf_id %d is not an LDS granule buffer" % buf_id)
        for i, v in enumerate(words):
            self.lds[(offset * GRAN_WORDS + i) % len(self.lds)] = v & MASK
        if count:
            k = 0 if buf_id == 0 else buf_id - 8
            self.counters[k] = (self.counters[k] + len(words) // GRAN_WORDS) & MASK

    def receive_credit(self, amount):
        """A CREDIT signal: its amount advances counter 3."""
        self.counters[3] = (self.counters[3] + (amount & 0xFFFF)) & MASK

    # ---- memory ---------------------------------------------------------
    def _word(self, a, pc):
        r = region(a)
        if r == R_LDS:
            return self.lds[((a & 0x0FFF_FFFF) >> 2) % len(self.lds)]
        if r == R_SPAD:
            return self.spad[((a & 0x0FFF_FFFF) >> 2) % len(self.spad)]
        if r == R_DRAM:
            return self.dram.get(a & ~3, 0)
        if r == R_CTL:
            return 0
        raise Halt(CAUSE_FAULT, pc)

    def _store(self, a, val, be, pc):
        r = region(a)
        base = a & ~3
        if r == R_LDS:
            arr, i = self.lds, ((a & 0x0FFF_FFFF) >> 2) % len(self.lds)
        elif r == R_SPAD:
            arr, i = self.spad, ((a & 0x0FFF_FFFF) >> 2) % len(self.spad)
        elif r == R_DRAM:
            arr, i = self.dram, base
        else:
            raise Halt(CAUSE_FAULT, pc)
        old = arr.get(i, 0) if isinstance(arr, dict) else arr[i]
        new = 0
        for b in range(4):
            src = val if (be >> b) & 1 else old
            new |= ((src >> (8 * b)) & 0xFF) << (8 * b)
        arr[i] = new & MASK

    def _check_access(self, addrs, nby, pc):
        """compute-unit.md s5.1: one per-lane access is all DRAM or all LDS, every
        lane aligned for its width; anything else faults before any lane acts."""
        regions = {region(a) for a in addrs}
        if (regions - {R_DRAM}) and (regions - {R_LDS}):
            raise Halt(CAUSE_FAULT, pc)
        if any(a & (nby - 1) for a in addrs):
            raise Halt(CAUSE_FAULT, pc)

    def load(self, a, width, signed, pc):
        if width == 2 and (a & 1):
            raise Halt(CAUSE_FAULT, pc)
        if width == 4 and (a & 3):
            raise Halt(CAUSE_FAULT, pc)
        w = self._word(a, pc)
        if width == 1:
            v = (w >> (8 * (a & 3))) & 0xFF
            return sx(v, 8) & MASK if signed else v
        if width == 2:
            v = (w >> (16 * ((a >> 1) & 1))) & 0xFFFF
            return sx(v, 16) & MASK if signed else v
        return w & MASK

    def store(self, a, val, width, pc):
        if width == 2 and (a & 1):
            raise Halt(CAUSE_FAULT, pc)
        if width == 4 and (a & 3):
            raise Halt(CAUSE_FAULT, pc)
        if width == 1:
            self._store(a, (val & 0xFF) * 0x0101_0101, 1 << (a & 3), pc)
        elif width == 2:
            self._store(
                a, (val & 0xFFFF) * 0x0001_0001, 0b1100 if (a & 2) else 0b0011, pc
            )
        else:
            self._store(a, val, 0xF, pc)

    # ---- one instruction, across the active lanes ------------------------
    def step(self, w):
        pc = w.pc
        if (pc >> 2) >= len(self.imem):
            raise Halt(CAUSE_FAULT, pc)
        ins = self.imem[pc >> 2]
        opc = ins & 0x7F
        w.pc = (pc + 4) & MASK
        self.instret += 1
        if opc == OPC_KHG:
            return self._khg(w, ins, pc)
        if opc == OPC_KHGI:
            return self._khgi(w, ins, pc)
        return self._rv32(w, ins, pc)

    def _rv32(self, w, ins, pc):
        """Ordinary RV32I, applied to every ACTIVE lane of the per-thread file."""
        rd = (ins >> 7) & 0x1F
        f3 = (ins >> 12) & 7
        rs1 = (ins >> 15) & 0x1F
        rs2 = (ins >> 20) & 0x1F
        f7 = (ins >> 25) & 0x7F
        opc = ins & 0x7F
        imm_i = sx(ins >> 20, 12)
        imm_s = sx(((ins >> 25) << 5) | ((ins >> 7) & 0x1F), 12)
        imm_u = ins & 0xFFFF_F000
        imm_j = sx(
            (((ins >> 31) & 1) << 20)
            | (((ins >> 12) & 0xFF) << 12)
            | (((ins >> 20) & 1) << 11)
            | (((ins >> 21) & 0x3FF) << 1),
            21,
        )

        # A per-thread condition reaching one PC is undefined, so the encoding
        # refuses it rather than promising the compiler proved uniformity.
        if opc == 0x63:
            raise Halt(CAUSE_FAULT, pc)
        if opc == 0x73:
            if (ins >> 21) or ((ins >> 7) & 0x1FFF):
                raise Halt(CAUSE_FAULT, pc)
            # ONE WAVE RETIRES, NOT THE UNIT. A dispatch is finished when its
            # LAST wave is; a fault, by contrast, is a property of the program
            # and still kills everything. The halt word and cause are the last
            # retiring wave's, which is what the hardware's a0 snoop records.
            w.done = True
            self.cause = CAUSE_EBREAK if (ins >> 20) & 1 else CAUSE_ECALL
            self.halt_word = w.x[10][0]
            return (pc, 0, {})
        if opc == 0x0F:
            return None
        if opc in (0x03, 0x23):
            if (opc == 0x03 and f3 in (3, 6, 7)) or (opc == 0x23 and f3 > 2):
                raise Halt(CAUSE_FAULT, pc)
            imm = imm_i if opc == 0x03 else imm_s
            self._check_access(
                [(w.x[rs1][ln] + imm) & MASK for ln in w.active()], 1 << (f3 & 3), pc
            )

        vals = {}
        for ln in w.active():
            a, b = w.x[rs1][ln], w.x[rs2][ln]
            if opc == 0x37:
                v = imm_u
            elif opc == 0x17:
                v = (pc + imm_u) & MASK
            elif opc == 0x6F:
                v = (pc + 4) & MASK
                w.pc = (pc + imm_j) & MASK
            elif opc == 0x67:
                v = (pc + 4) & MASK
                w.pc = (a + imm_i) & MASK & ~1
            elif opc == 0x03:
                if f3 in (3, 6, 7):
                    raise Halt(CAUSE_FAULT, pc)
                ad = (a + imm_i) & MASK
                self._note(ad, w, ln)
                v = self.load(ad, {0: 1, 1: 2, 2: 4, 4: 1, 5: 2}[f3], f3 in (0, 1), pc)
            elif opc == 0x23:
                if f3 > 2:
                    raise Halt(CAUSE_FAULT, pc)
                ad = (a + imm_s) & MASK
                self._note(ad, w, ln)
                self.store(ad, b, 1 << f3, pc)
                v = None
            elif opc == 0x33 and f7 == 0x01:
                # RV32M. ONE 33x33 SIGNED PRODUCT SERVES ALL FOUR: only the
                # extension bits differ, and `mul`'s low half does not depend on
                # them at all. Getting the sign extension wrong here is the easy
                # mistake, so the three high forms are spelled out separately.
                if f3 > 3:
                    raise Halt(CAUSE_FAULT, pc)  # div/rem stay illegal
                sa, sb = sx(a, 32), sx(b, 32)
                if f3 == 0:
                    v = (sa * sb) & MASK  # mul: low half
                elif f3 == 1:
                    v = ((sa * sb) >> 32) & MASK  # mulh: signed x signed
                elif f3 == 2:
                    v = ((sa * b) >> 32) & MASK  # mulhsu: signed x unsigned
                else:
                    v = ((a * b) >> 32) & MASK  # mulhu: unsigned
            elif opc in (0x13, 0x33):
                second = imm_i & MASK if opc == 0x13 else b
                sh = (imm_i & 0x1F) if opc == 0x13 else (b & 0x1F)
                sub = opc == 0x33 and f7 == 0x20
                if f3 == 0:
                    v = (a - b) & MASK if sub else (a + second) & MASK
                elif f3 == 1:
                    v = (a << sh) & MASK
                elif f3 == 2:
                    v = 1 if sx(a, 32) < sx(second, 32) else 0
                elif f3 == 3:
                    v = 1 if a < second else 0
                elif f3 == 4:
                    v = a ^ second
                elif f3 == 5:
                    v = (sx(a, 32) >> sh) & MASK if f7 == 0x20 else (a >> sh)
                elif f3 == 6:
                    v = a | second
                else:
                    v = a & second
            else:
                raise Halt(CAUSE_FAULT, pc)
            if v is not None:
                vals[ln] = v & MASK

        if rd != 0:
            for ln, v in vals.items():
                w.x[rd][ln] = v
            return (pc, rd, dict(vals))
        return (pc, 0, {})

    def _note(self, addr, w, ln):
        """Record one lane's address so a gather's request count is countable."""
        if self.reqs and self.reqs[-1][0] == (w.wid, w.pc):
            self.reqs[-1][1].append(addr)
        else:
            self.reqs.append(((w.wid, w.pc), [addr]))

    def _khg(self, w, ins, pc):
        """custom-2: the R-type groups."""
        rd = (ins >> 7) & 0x1F
        f3 = (ins >> 12) & 7
        rs1 = (ins >> 15) & 0x1F
        rs2 = (ins >> 20) & 0x1F
        f7 = (ins >> 25) & 0x7F

        if f3 == 0:  # SALU
            a, b = w.s[rs1], w.s[rs2]
            v = {
                0: (a + b) & MASK,
                1: (a - b) & MASK,
                2: (a << (b & 31)) & MASK,
                3: 1 if sx(a, 32) < sx(b, 32) else 0,
                4: 1 if a < b else 0,
                5: a ^ b,
                6: a >> (b & 31),
                7: (sx(a, 32) >> (b & 31)) & MASK,
                8: a | b,
                9: a & b,
            }.get(f7)
            if v is None:
                raise Halt(CAUSE_FAULT, pc)
            if rd:
                w.s[rd] = v & MASK
            return (pc, -rd, w.s[rd] if rd else 0)

        if f3 == 1:  # SMOV
            if f7 == 0:
                for ln in w.active():
                    w.x[rd][ln] = w.s[rs1]
                return (pc, rd, {ln: w.s[rs1] for ln in w.active()})
            if f7 == 1:
                # Slot 5 is THIS WAVE'S id, so it cannot come from the flat
                # control table -- it is the only way a wave learns which of the
                # dispatch it is, and every per-wave address derives from it.
                if rd:
                    w.s[rd] = self._rdctl(w, rs2) & MASK
                return (pc, -rd, w.s[rd] if rd else 0)
            if f7 == 2:
                a = w.s[rs1]
                if region(a) != R_DRAM or (a & 3):
                    raise Halt(CAUSE_FAULT, pc)
                if rd:
                    w.s[rd] = self.dram.get(a, 0) & MASK
                return (pc, -rd, w.s[rd] if rd else 0)
            raise Halt(CAUSE_FAULT, pc)

        if f3 == 2:  # DIV
            if f7 == 0:
                t = sum(1 << ln for ln in w.active() if w.x[rs1][ln] != 0)
                f = w.mask & ~t
                w.push(w.mask)
                w.push(f)
                w.mask = t
                return (pc, 0, {})
            if f7 == 1:
                w.mask = w.pop()
                return (pc, 0, {})
            if f7 == 2:
                w.mask = w.s[rs1] & ((1 << self.lanes) - 1)
                if w.mask == 0:
                    w.done = True
                return (pc, 0, {})
            if f7 == 3:
                # The barrier EXECUTES: the wave sleeps until every live wave of
                # its workgroup has arrived (compute-unit.md s7.2).
                w.sleep = ("bar",)
                return (pc, 0, {})
            raise Halt(CAUSE_FAULT, pc)

        if f3 == 3:  # SUB
            act = w.active()
            if f7 == 0:
                src = {ln: w.x[rs1][ln] for ln in act}
                out = {
                    ln: src.get(ln ^ (w.s[rs2] & (self.lanes - 1)), w.x[rs1][ln])
                    for ln in act
                }
                for ln, v in out.items():
                    w.x[rd][ln] = v
                return (pc, rd, out)
            if f7 == 1:
                v = w.x[rs1][rs2 % self.lanes]
                for ln in act:
                    w.x[rd][ln] = v
                return (pc, rd, {ln: v for ln in act})
            if f7 == 2:
                v = sum(1 << ln for ln in act if w.x[rs1][ln] != 0)
                if rd:
                    w.s[rd] = v
                return (pc, -rd, v)
            if f7 == 9:
                for ln in act:
                    w.x[rd][ln] = ln
                return (pc, rd, {ln: ln for ln in act})
            if f7 == 8:
                # The LOWEST ACTIVE lane, never lane 0. An all-zero mask is not
                # a defined case; the scheduler must not issue such a wave.
                assert act, "vreadfirst issued on an all-zero active mask"
                v = w.x[rs1][act[0]]
                if rd:
                    w.s[rd] = v
                return (pc, -rd, v)
            vals = [sx(w.x[rs1][ln], 32) for ln in act]
            if f7 == 3:
                v = sum(vals) & MASK
            elif f7 == 4:
                v = (max(vals) if vals else 0) & MASK
            elif f7 == 5:
                v = (min(vals) if vals else 0) & MASK
            elif f7 == 6:
                v = MASK
                for ln in act:
                    v &= w.x[rs1][ln]
            elif f7 == 7:
                v = 0
                for ln in act:
                    v |= w.x[rs1][ln]
            else:
                raise Halt(CAUSE_FAULT, pc)
            if rd:
                w.s[rd] = v & MASK
            return (pc, -rd, v & MASK)

        if f3 == 4:  # VMEM
            op, scale, width = (f7 >> 4) & 7, (f7 >> 2) & 3, f7 & 3
            if op == 6:
                return self._amo(w, rd, rs1, rs2, scale, width, pc)
            if width > 2 or op > 5:
                raise Halt(CAUSE_FAULT, pc)
            nby = 1 << width
            lin = op >= 3
            store = op in (2, 5)
            signed = op in (0, 3)
            # Offsets are SIGNED and the sum wraps at 32 bits, both pinned.
            addrs = {
                ln: (w.s[rs1] + ((ln if lin else sx(w.x[rs2][ln], 32)) << scale)) & MASK
                for ln in w.active()
            }
            if addrs:
                self.reqs.append(((w.wid, pc), list(addrs.values())))
            self._check_access(list(addrs.values()), nby, pc)
            if store:
                for ln, ad in addrs.items():
                    self.store(ad, w.x[rd][ln], nby, pc)
                return (pc, 0, {})
            out = {ln: self.load(ad, nby, signed, pc) for ln, ad in addrs.items()}
            for ln, v in out.items():
                w.x[rd][ln] = v
            return (pc, rd, out)

        if f3 == 5:  # FLT (G9)
            # THE MODEL IS THE SIMD TIER'S, NOT A SECOND ONE. `KF.fpu` is
            # `rv_fpu` as built -- alignment window, clamped shift,
            # uncomplemented sticky and all -- so this is bit-exact against the
            # hardware for the same reason the arithmetic was not forked.
            # funct7[2:0] is the operation: 0-3 the arithmetic four, 4-7 the
            # FSFU seeds. FP32 is the only compute type, so there is no format
            # bit and nothing above 7 is mapped.
            if f7 > 7:
                raise Halt(CAUSE_FAULT, pc)
            if (f7 & 4) and not self.has_fsfu:
                raise Halt(CAUSE_FAULT, pc)
            out = {}
            for ln in w.active():
                av = w.x[rs1][ln] & MASK
                bv = w.x[rs2][ln] & MASK
                dv = w.x[rd][ln] & MASK
                if f7 & 4:
                    out[ln] = KF.seed(f7 & 3, av, KSEED.TAB)
                    continue
                op = f7 & 3
                if op == 1:  # vfmul
                    fop, sc = KF.OP_MUL, 0
                elif op == 2:  # vfadd
                    fop, sc = KF.OP_ADD, bv
                elif op == 3:  # vfsub
                    fop, sc = KF.OP_SUB, bv
                else:  # vfma
                    fop, sc = KF.OP_FMA, dv
                out[ln] = KF.fpu(fop, av, bv, sc)[0]
            for ln, v in out.items():
                w.x[rd][ln] = v
            return (pc, rd, out)

        if f3 == 6:  # VX
            out = {}
            for ln in w.active():
                a, b = w.x[rs1][ln] & MASK, w.x[rs2][ln] & MASK
                if f7 == 0:
                    v = f32_minmax(a, b, want_max=False)
                elif f7 == 1:
                    v = f32_minmax(a, b, want_max=True)
                elif f7 in (2, 3, 4):
                    v = f32_cmp(a, b, ("lt", "le", "eq")[f7 - 2])
                elif f7 == 5:
                    if rs2 > 1:
                        raise Halt(CAUSE_FAULT, pc)
                    v = f16_to_f32((a >> (16 * rs2)) & 0xFFFF)
                elif f7 == 6:
                    v = (f32_to_f16(b) << 16) | f32_to_f16(a)
                else:
                    raise Halt(CAUSE_FAULT, pc)
                out[ln] = v & MASK
            if rd:
                for ln, v in out.items():
                    w.x[rd][ln] = v
                return (pc, rd, out)
            return (pc, 0, {})

        if f3 == 7:  # STRM
            return self._strm(w, f7, rs1, rs2, pc)

        raise Halt(CAUSE_FAULT, pc)

    # ---- control slots (compute-unit.md s2.2) -----------------------------
    def _rdctl(self, w, idx):
        if idx == 5:
            return w.wid
        if idx == 6:
            return w.wg
        if idx == 7:
            return w.wiw
        if idx == 8:
            return self.wg_count
        if idx == 9:
            return self.wg_waves
        if idx == 10:
            return LDS_BASE + w.slot * self.wg_lds * LINE_BYTES
        if idx == 11:
            return self.launch_id
        if 16 <= idx < 16 + len(self.counters):
            return self.counters[idx - 16]
        return self.ctl[idx]

    # ---- LDS atomics (compute-unit.md s7.1) -------------------------------
    def _amo(self, w, rd, rs1, rs2, scale, amo, pc):
        """Lanes in ascending order: the resolver serves the lowest lane first, so
        lanes on one address see each other's results in lane order."""
        out = {}
        for ln in w.active():
            a = (w.s[rs1] + (sx(w.x[rs2][ln], 32) << scale)) & MASK
            if region(a) != R_LDS or (a & 3):
                raise Halt(CAUSE_FAULT, pc)
            i = ((a & 0x0FFF_FFFF) >> 2) % len(self.lds)
            old, v = self.lds[i], w.x[rd][ln] & MASK
            if amo == 0:
                new = (old + v) & MASK
            elif amo == 1:
                new = old if sx(old, 32) >= sx(v, 32) else v
            elif amo == 2:
                new = old if sx(old, 32) <= sx(v, 32) else v
            else:
                new = v
            self.lds[i] = new
            out[ln] = old
        if rd:
            for ln, v in out.items():
                w.x[rd][ln] = v
            return (pc, rd, out)
        return (pc, 0, {})

    # ---- the stream engine (compute-unit.md s6) ---------------------------
    def _desc(self, d, pc):
        off, n, k = d & 0xFFFF, ((d >> 16) & 0xFF) + 1, (d >> 24) & 3
        if d >> 26:
            raise Halt(CAUSE_FAULT, pc)
        if (off + n) * GRAN_WORDS > len(self.lds):
            raise Halt(CAUSE_FAULT, pc)
        return off, n, k

    def _strm(self, w, f7, rs1, rs2, pc):
        """Streams complete at issue here. The hardware completes them later, and a
        shader that reads streamed data without `wait`ing for its counter is wrong
        on the hardware and right here -- so every stream test waits."""
        s1 = w.s[rs1]
        if f7 in (0, 1):
            off, n, k = self._desc(w.s[rs2], pc)
            if region(s1) != R_DRAM or (s1 & (LINE_BYTES - 1)):
                raise Halt(CAUSE_FAULT, pc)
            for g in range(n):
                for i in range(GRAN_WORDS):
                    li = (off + g) * GRAN_WORDS + i
                    da = (s1 + g * LINE_BYTES + 4 * i) & MASK
                    if f7 == 0:
                        self.lds[li] = self.dram.get(da, 0)
                    else:
                        self.dram[da] = self.lds[li]
            self.counters[k] = (self.counters[k] + n) & MASK
            return (pc, 0, {})
        if f7 == 2:
            off, n, k = self._desc(w.s[rs2], pc)
            words = self.lds[off * GRAN_WORDS : (off + n) * GRAN_WORDS]
            self.sent.append(
                {
                    "y": (s1 >> 28) & 0xF,
                    "x": (s1 >> 24) & 0xF,
                    "buf_id": (s1 >> 16) & 0xFF,
                    "offset": s1 & 0xFFFF,
                    "words": list(words),
                }
            )
            self.counters[k] = (self.counters[k] + n) & MASK
            return (pc, 0, {})
        if f7 == 3:
            self.credits.append((s1 & 0xFF, w.s[rs2] & 0xFFFF))
            return (pc, 0, {})
        if f7 == 4:
            if rs2 >= len(self.counters):
                raise Halt(CAUSE_FAULT, pc)
            if self.counters[rs2] < s1:
                w.sleep = ("wait", rs2, s1)
            return (pc, 0, {})
        if f7 == 5:
            if rs2 >= len(self.counters):
                raise Halt(CAUSE_FAULT, pc)
            self.counters[rs2] = s1 & MASK
            return (pc, 0, {})
        raise Halt(CAUSE_FAULT, pc)

    def _khgi(self, w, ins, pc):
        """custom-3: the I-type groups. Scalar, so one value and no mask."""
        rd = (ins >> 7) & 0x1F
        f3 = (ins >> 12) & 7
        rs1 = (ins >> 15) & 0x1F
        imm = sx(ins >> 20, 12)
        a = w.s[rs1]
        if f3 == 6:
            if a == 0:
                w.pc = (pc + imm) & MASK
            return (pc, 0, 0)
        if f3 == 7:
            if a != 0:
                w.pc = (pc + imm) & MASK
            return (pc, 0, 0)
        v = {
            0: (a + imm) & MASK,
            1: a & (imm & MASK),
            2: a | (imm & MASK),
            3: (a << (imm & 31)) & MASK,
            4: a >> (imm & 31),
            5: (sx(a, 32) >> (imm & 31)) & MASK,
        }[f3]
        if rd:
            w.s[rd] = v & MASK
        return (pc, -rd, v & MASK)

    # ---- the scheduler ---------------------------------------------------
    def run(self, limit=2_000_000):
        """Round-robin over ready waves until one halts or all are done.

        Ready-wave interleaving, not a compulsory barrel period: with four ready
        waves the model issues from four, not from sixteen with twelve bubbles.
        """
        trace = []
        try:
            for _ in range(limit):
                self._fill_slots()
                live = [w for w in self.waves if not w.done]
                if not live:
                    return trace, self.cause or CAUSE_ECALL, self.halt_word
                self._wake(live)
                ran = False
                for w in live:
                    if w.done or w.sleep:
                        continue
                    trace.append((w.wid,) + self.step(w))
                    ran = True
                if not ran:
                    why = sorted({w.sleep for w in live})
                    raise RuntimeError("deadlock: every live wave sleeps on %s" % why)
            raise RuntimeError("model did not halt within %d steps" % limit)
        except Halt as h:
            self.cause = h.cause
            self.halt_word = h.word
            return trace, h.cause, h.word

    def _wake(self, live):
        """Clear every sleep whose reason has gone (compute-unit.md s4.2)."""
        for w in live:
            if (
                w.sleep
                and w.sleep[0] == "wait"
                and self.counters[w.sleep[1]] >= w.sleep[2]
            ):
                w.sleep = None
        # A barrier releases when every live wave of the workgroup has arrived; a
        # wave that has ended counts as arrived because it is no longer live.
        for s in {w.slot for w in live}:
            group = [w for w in live if w.slot == s]
            if group and all(w.sleep == ("bar",) for w in group):
                for w in group:
                    w.sleep = None

    def fills_issued(self):
        """Distinct 32-byte lines the recorded accesses would have requested."""
        return sum(len(coalesce(a)) for _, a in self.reqs)
