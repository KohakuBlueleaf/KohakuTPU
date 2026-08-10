"""Address map, control-program ISA, and the transport abstraction.

The transport is the whole point of this file. A driver written against
``write64`` / ``read64`` runs unchanged against a simulator, a JTAG-AXI master,
or XDMA -- only the backend differs. Everything above it is ordinary software.

See docs/mas/driver.md.
"""

import abc
import time
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Address map. Bit 28 selects MAG over the main orchestrator.
# --------------------------------------------------------------------------
ORC_BASE = 0x0000_0000
MAG_BASE = 0x1000_0000

# main orchestrator
MO_CTRL = 0x0000  # W: [0] GO      R: [0] busy [1] done [2] err
MO_PC = 0x0008  # R: current command index
MO_CODE = 0x0010  # R: the DONE code
MO_POLLS = 0x0018  # R: polls executed
MO_CMD = 0x1000  # command n, field f at MO_CMD + n*32 + f*8

# the agent inside MAG (the existing orchestrator register map)
A_CTRL = 0x0000
A_STATUS = 0x0008  # R: [0] busy [1] error [2] mesh_ready
# R: {GRID_HI, GRID_LO, POS_WIDTH, FLIT_WIDTH}, one byte each but the width.
# The only register whose right answer is known before the machine has run.
A_CAPS = 0x0010
A_PROG_DST = 0x0040
A_PROG_LEN = 0x0048
A_PROG_KICK = 0x0050
A_PROG_STAT = 0x0058  # R: [0] run, [16:1] flits left, [32:17] credit
A_PROG_CRED = 0x0060
A_PROG_BASE = 0x0068  # first staging slot of this program
A_SIG_DONE = 0x0070  # R: completions from every node; W: clear
A_NODE = 0x1000  # + {y,x}*8
A_STAGE = 0x2000  # instruction flits, 5 x 64-bit words each

# The raw-flit mailbox: the only path to a node that is not a dispatch, and so
# the only way to ask a CU what it is rather than assuming.
A_TX_FLIT0 = 0x0100  # 5 x 64-bit, low word first
A_TX_KICK = 0x0140
A_TX_STATUS = 0x0148  # R: [16] tx_full
A_RX_FLIT0 = 0x0180
A_RX_POP = 0x01C0
A_RX_STATUS = 0x01C8  # R: [16] empty [17] overflow

# noc_cu_base flit types and the two mandatory control registers every endpoint
# answers, whatever it computes.
T_CU_CTRL = 0x7
CU_CAPS = 0  # {CU_TYPE[16], CU_VERSION[8], N_BUFFERS[4], INST_DEPTH[16], 20'd0}
CU_STATUS = 1
CU_COUNTERS = 2  # {instructions_retired[32], busy_cycles[32]}, kept in the base
CU_DBG = 3  # the datapath's own pair; what it means is per CU_TYPE
CTRL_READ_RESP = 2

# CU_TYPE is two ASCII characters, so an unknown endpoint is readable.
CU_MATMUL = 0x4D47  # 'MG', mx_cluster_cu
CU_VECTOR = 0x5643  # 'VC', vec_cu

# 0x8, NOT the 0x4 that docs/noc/spec.md printed until recently: 0x4 is
# MEM_WR_DATA, and a CU_DATA flit carrying it entered MAG's write queue as data.
T_CU_DATA = 0x8
T_CU_INST = 0x5

# CU_SIGNAL codes, centrally allocated below 0x40.
SIG_INST_COMPLETE = 0x00
SIG_BATCH_COMPLETE = 0x01
SIG_DATA_RECEIVED = 0x03
SIG_FAULT = 0x04

# A CU_DATA burst is a descriptor flit then `len+1` pure data flits, so this is
# the longest one the 8-bit length field can describe.
CUD_MAX_WORDS = 256

FLIT_BITS = 288
POS_BITS = 4

# control-program opcodes
OP_WR = 1
OP_POLL = 2
OP_DONE = 3

FLIT_WORDS = 5  # 64-bit words per staged instruction flit

WORD_BYTES = 8  # the transport's word: every address below is a byte address
MASK64 = (1 << 64) - 1
MASK32 = (1 << 32) - 1

# noc_cu_base INST_DEPTH. Credit must never allow more instructions in flight
# than one CU's instruction FIFO can hold -- see Program.seed_credits.
INST_DEPTH = 32


def node_index(x: int, y: int) -> int:
    """NoC coordinate -> the {y,x} byte used by PROG_DST and NODE_STATUS."""
    return ((y & 0xF) << 4) | (x & 0xF)


def _field(flit: int, hi: int, width: int) -> int:
    """Verilog's ``flit[hi -: width]``, counting from the MSB as the RTL does."""
    return (flit >> (hi - width + 1)) & ((1 << width) - 1)


def ctrl_request(dst, src, idx: int, txn: int = 1) -> int:
    """A CU_CTRL read flit: ask node `dst` for control register `idx`.

    The header is noc_cu_base's HDR_* macros, which count DOWN from the flit's
    most significant bit -- {DX, DY, SX, SY, TYPE, ID, LAST, ...} -- so this is
    assembled the same way rather than from a struct that happens to match.
    `src` is where the reply goes, which for us is always the agent.
    """
    parts = [
        (dst[0], POS_BITS),
        (dst[1], POS_BITS),
        (src[0], POS_BITS),
        (src[1], POS_BITS),
        (T_CU_CTRL, 4),
        (txn, 8),
        (1, 1),  # LAST
        (0, 3),
        (0, 8),  # opcode; the CU reads the index and answers regardless
        (idx, 8),
    ]
    flit, at = 0, FLIT_BITS
    for value, width in parts:
        at -= width
        flit |= (value & ((1 << width) - 1)) << at
    return flit


def node_status_addr(x: int, y: int) -> int:
    """Byte address of one node's NODE_STATUS mirror."""
    return MAG_BASE + A_NODE + node_index(x, y) * 8


def _header(ty: int, txn: int, last: bool, dst=None, src=None) -> int:
    """The 32-bit routing header, MSB-first as `noc_pkt.vh` lays it out.

    `dst` and `src` default to zero because the DISPATCHER stamps both
    (`noc_orchestrator.v` s"the dispatcher owns the routing header"): it
    overwrites destination from PROG_DST and source with its own coordinates,
    and leaves type, txn, last and payload exactly as staged. Pass them only
    for the raw mailbox path, which stamps nothing.
    """
    dst = dst or (0, 0)
    src = src or (0, 0)
    parts = [
        (dst[0], POS_BITS),
        (dst[1], POS_BITS),
        (src[0], POS_BITS),
        (src[1], POS_BITS),
        (ty, 4),
        (txn, 8),
        (1 if last else 0, 1),
        (0, 3),
    ]
    flit, at = 0, FLIT_BITS
    for value, width in parts:
        at -= width
        flit |= (value & ((1 << width) - 1)) << at
    return flit


def cu_data_flits(
    words, buf_id=0, offset=0, signal=True, txn=0, ack=None, dst=None, src=None
):
    """A whole `CU_DATA` burst: the descriptor flit, then one flit per word.

    `words` is a sequence of 256-bit payloads, at most :data:`CUD_MAX_WORDS`
    because `len` is 8 bits holding count-minus-one. `offset` is the
    destination start in 32-BYTE GRANULES, which is one L1 word, and `buf_id`
    names the destination buffer -- CU-defined, and a vector core has one flat
    L1 and faults on anything but 0. `signal` sets `signal_on_complete`, which
    is what makes the transfer observable at all.

    `ack` is where that completion is sent, as an `(x, y)` coordinate; None
    sends it to the descriptor's source. A CU answering its SENDER is useless
    when the sender is another CU -- nothing there consumes it -- so a peer
    transfer points this at the orchestrator and the host waits on
    `NODE_STATUS[orchestrator]`. The payload stays on the mesh; only the
    completion is routed somewhere the sequencer can see it.

    Returns the flits in order, `last` set on the final one. Raises
    :class:`ValueError` if the burst is empty, longer than the length field can
    describe, or if any field does not fit -- `ack` included, since `(0, 0)` is
    the "reply to the sender" sentinel and is never a real endpoint (it is a
    mesh corner, which touches no router).
    """
    words = list(words)
    if not words:
        raise ValueError("a CU_DATA burst carries at least one data flit")
    if len(words) > CUD_MAX_WORDS:
        raise ValueError(
            f"{len(words)} words: `len` is 8 bits holding count-1, so a burst "
            f"carries at most {CUD_MAX_WORDS}. Split it."
        )
    for name, value, width in (
        ("buf_id", buf_id, 8),
        ("offset", offset, 16),
        ("txn", txn, 8),
    ):
        if not isinstance(value, int) or value < 0 or value >> width:
            raise ValueError(f"{name} = {value!r} does not fit {width} bits")

    ack_field = 0
    if ack is not None:
        ax, ay = ack
        for name, value in (("ack x", ax), ("ack y", ay)):
            if not isinstance(value, int) or value < 0 or value >> POS_BITS:
                raise ValueError(f"{name} = {value!r} does not fit {POS_BITS} bits")
        if (ax, ay) == (0, 0):
            raise ValueError(
                "ack (0,0) is the 'reply to the sender' sentinel, and no endpoint "
                "can live there -- it is a mesh corner. Pass ack=None for that."
            )
        ack_field = (ay << 4) | ax

    desc = (
        (buf_id << 248)
        | (offset << 232)
        | ((len(words) - 1) << 224)
        | ((1 if signal else 0) << 216)
        | (ack_field << 208)
    )
    out = [_header(T_CU_DATA, txn, False, dst, src) | desc]
    for i, w in enumerate(words):
        out.append(
            _header(T_CU_DATA, txn, i == len(words) - 1, dst, src)
            | (w & ((1 << 256) - 1))
        )
    return out


def ctrl_reply(flit: int) -> dict:
    """Decode a CU_CTRL read response: who answered, which register, what value."""
    return {
        "src": (_field(flit, 279, POS_BITS), _field(flit, 275, POS_BITS)),
        "type": _field(flit, 271, 4),
        "txn": _field(flit, 267, 8),
        "op": _field(flit, 255, 8),
        "idx": _field(flit, 247, 8),
        "value": _field(flit, 239, 64),
    }


def decode_caps(word: int) -> dict:
    """CU_CAPS: what an endpoint says it is, before anything is dispatched to it.

    ``{CU_TYPE[16], CU_VERSION[8], N_BUFFERS[4], INST_DEPTH[16], 20'd0}``. The
    type is two ASCII characters -- 'MG' a matmul cluster, 'VC' a vector core --
    so an unknown node reports something readable rather than a number.
    """
    kind = (word >> 48) & 0xFFFF
    return {
        "type": kind,
        "name": bytes([(kind >> 8) & 0xFF, kind & 0xFF]).decode("ascii", "replace"),
        "version": (word >> 40) & 0xFF,
        "buffers": (word >> 36) & 0xF,
        "inst_depth": (word >> 20) & 0xFFFF,
    }


def decode_status(word: int) -> dict:
    """CU_STATUS: ``{busy, error, 14'd0, inst_space[16], 32'd0}``.

    `inst_space` is the instruction FIFO's FREE entries. At zero the dispatcher
    is being held off, which is a different problem from a slow datapath and
    looks identical in wall clock.

    The CU's own error flag is `cu_error` and NOT `error`: a bare `error` key
    is this package's sentinel for a node that did not answer at all, and the
    two colliding made every reachable node look like a failed read.
    """
    return {
        "busy": (word >> 63) & 1,
        "cu_error": (word >> 62) & 1,
        "inst_space": (word >> 32) & 0xFFFF,
    }


def decode_counters(word: int) -> dict:
    """CU_CTRL index 2: ``{instructions_retired[32], busy_cycles[32]}``.

    Counted in `noc_cu_base`, so every endpoint reports these identically
    whatever it computes -- that is the point of them living there.

    BOTH ACCUMULATE SINCE `resetn` AND NEITHER CAN BE CLEARED, so a measurement
    is the difference between two reads, not a single one. They are 32 bits at
    a few hundred MHz, which is a wrap every ten seconds or so; differences
    must be taken modulo 2**32 (:func:`ktpu.hw.fpga.delta_counters`).
    """
    return {"retired": (word >> 32) & MASK32, "busy_cycles": word & MASK32}


def decode_dbg(word: int, cu_type: int) -> dict:
    """CU_CTRL index 3: the datapath's own pair, decoded for `cu_type`.

    `mx_cluster_cu` reports ``{compute_cycles, memory_cycles}`` -- the array
    running (`gemm_busy || sweep_busy`) against `S_FILL` waiting on operands.
    Both are free-running and INDEPENDENT, so they overlap whenever a fill and
    a GEMM are in flight together and their sum is not a total.

    `vec_cu` reports its kernel's cycle count, and that one is **per run**:
    `vec_core` clears it at every RUN, so it describes the last kernel rather
    than the session and must not be differenced. `scope` says which is which.
    Every other endpoint ties the port to zero and reports None.
    """
    if cu_type == CU_MATMUL:
        return {
            "compute_cycles": (word >> 32) & MASK32,
            "memory_cycles": word & MASK32,
            "scope": "cumulative",
        }
    if cu_type == CU_VECTOR:
        return {
            "compute_cycles": word & MASK32,
            "memory_cycles": None,
            "scope": "last-run",
        }
    return {"compute_cycles": None, "memory_cycles": None, "scope": None}


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------
class TransportUnavailable(RuntimeError):
    """This backend cannot be reached, and the message says why.

    Distinct from an I/O failure ON a backend that opened: absence is a
    configuration answer -- wrong host, driver not installed, card not
    enumerated -- and a caller that can fall back to another transport needs
    to tell the two apart without reading a win32 code.
    """


class Transport(abc.ABC):
    """A 64-bit AXI window. Two methods is the entire hardware dependency.

    THE BLOCK METHODS ARE NOT A SECOND CONTRACT. A block at `addr` must be
    indistinguishable from `len(data) // 8` word accesses at ascending
    addresses, little-endian -- which is exactly what they do by default, so a
    backend overrides them only when it has a transfer that beats the loop.
    That equivalence is what lets one test compare a bulk backend against the
    recorded word writes and expect the same bytes at the same addresses.

    `bulk` says whether overriding happened, because the CALLER has work to do
    either way: coalescing scattered writes into runs is worth the arithmetic
    over PCIe, where a block is one descriptor and the loop is one per word,
    and worth nothing over a transport that will only unpack it again.
    """

    bulk = False

    @abc.abstractmethod
    def write64(self, addr: int, data: int) -> None: ...

    @abc.abstractmethod
    def read64(self, addr: int) -> int: ...

    def write_block(self, addr: int, data: bytes) -> None:
        require_words(len(data))
        for off in range(0, len(data), WORD_BYTES):
            word = int.from_bytes(data[off : off + WORD_BYTES], "little")
            self.write64(addr + off, word)

    def read_block(self, addr: int, nbytes: int) -> bytes:
        require_words(nbytes)
        out = bytearray()
        for off in range(0, nbytes, WORD_BYTES):
            out += (self.read64(addr + off) & MASK64).to_bytes(WORD_BYTES, "little")
        return bytes(out)


def require_words(nbytes: int) -> None:
    """Raise unless `nbytes` is a whole number of 64-bit words.

    A partial word has no sequence of word accesses to be equivalent to, so
    there is nothing for a backend to fall back to and nothing to compare
    against. Refused at the boundary rather than truncated.
    """
    if nbytes % WORD_BYTES:
        raise ValueError(f"{nbytes} bytes is not a whole number of 64-bit words")


def runs(pairs):
    """Group word writes into contiguous ``(addr, bytes)`` blocks.

    A run breaks wherever the next address is not the last plus eight, so
    replaying the blocks writes the same bytes to the same addresses in the
    same order -- the grouping is an encoding of the list, not a reordering of
    it. `stage_flit` walks slots in address order, so a round's whole staging
    window collapses to one block: one DMA descriptor rather than six hundred.
    """
    out = []
    for addr, data in pairs:
        word = (data & MASK64).to_bytes(WORD_BYTES, "little")
        if out and addr == out[-1][0] + len(out[-1][1]):
            out[-1][1].extend(word)
        else:
            out.append((addr, bytearray(word)))
    return [(a, bytes(b)) for a, b in out]


class RecordingTransport(Transport):
    """Records writes instead of performing them.

    This is what makes most of the driver testable with no simulator at all:
    building a control program is a pure function, so it can be checked by
    comparing the recorded transactions against a known-good list.
    """

    def __init__(self) -> None:
        self.writes: list[tuple[int, int]] = []

    def write64(self, addr: int, data: int) -> None:
        self.writes.append((addr, data))

    def read64(self, addr: int) -> int:  # pragma: no cover - never used
        raise NotImplementedError("RecordingTransport cannot read")


class MemoryTransport(Transport):
    """A window backed by a dict, so a driver can be run with nothing attached.

    RecordingTransport cannot read, and half the driver -- every POLL, every
    result readback -- is reads. This holds what was written and hands it back.

    `on_read` maps an address to a callable taking the attempt count and
    returning the value. A register that only matches on the nth read is the
    one thing a static dict cannot express, and it is exactly what a POLL loop
    has to be tested against.
    """

    bulk = True

    def __init__(self, on_read=None) -> None:
        self.mem: dict[int, int] = {}
        self.on_read = dict(on_read or {})
        self.reads: dict[int, int] = {}
        self.blocks: list[tuple[int, int]] = []  # (addr, nbytes) of bulk accesses

    def write64(self, addr: int, data: int) -> None:
        _aligned(addr)
        self.mem[addr] = data & MASK64

    def read64(self, addr: int) -> int:
        _aligned(addr)
        n = self.reads[addr] = self.reads.get(addr, 0) + 1
        if addr in self.on_read:
            return self.on_read[addr](n) & MASK64
        return self.mem.get(addr, 0)

    def write_block(self, addr: int, data: bytes) -> None:
        require_words(len(data))
        self.blocks.append((addr, len(data)))
        for off in range(0, len(data), WORD_BYTES):
            self.write64(addr + off, int.from_bytes(data[off : off + 8], "little"))

    def read_block(self, addr: int, nbytes: int) -> bytes:
        require_words(nbytes)
        self.blocks.append((addr, nbytes))
        out = bytearray()
        for off in range(0, nbytes, WORD_BYTES):
            out += self.read64(addr + off).to_bytes(WORD_BYTES, "little")
        return bytes(out)


def _aligned(addr: int) -> None:
    """A word access to an unaligned address is a different access."""
    if addr % WORD_BYTES:
        raise ValueError(f"address {addr:#x} is not 64-bit aligned")


# --------------------------------------------------------------------------
# The control program
# --------------------------------------------------------------------------
@dataclass
class Command:
    op: int
    addr: int = 0
    data: int = 0
    mask: int = 0

    def words(self) -> tuple[int, int, int, int]:
        return (self.op, self.addr, self.data, self.mask)


@dataclass
class Program:
    """The machine's CONTROL program: dispatch, wait, halt.

    THE PROGRAM IS NOT A RECORDING OF EVERY AXI WRITE. Instruction flits and
    operand data are things the host uploads into the card's own memory before
    `GO`; putting them in the command RAM makes the host ship each flit twice
    -- once into the command RAM, and again when the command RAM replays it
    into the staging RAM -- and makes the program grow with the problem rather
    than with its control flow.

    So `setup` collects writes the host performs directly, and `cmds` holds
    only control. On a 2-cluster GEMM that is 53 commands down to 13 -- the 40
    flit words move to `setup` -- and the gap widens with every cluster.
    """

    cmds: list[Command] = field(default_factory=list)
    setup: list[tuple[int, int]] = field(default_factory=list)
    # What this program has already written to each register. Starts empty, so
    # a program never assumes a value it did not set itself -- rounds run as
    # separate programs and the first kick of each writes the full set.
    _shadow: dict[int, int] = field(default_factory=dict)

    # ---- setup: data the host writes itself, before GO -------------------
    def stage_flit(self, slot: int, flit: int) -> "Program":
        """Upload one 288-bit instruction flit into the staging RAM.

        Direct host writes, NOT commands. The staging RAM is already AXI
        addressable, so routing this through the command RAM would only add a
        copy.
        """
        for w in range(FLIT_WORDS):
            word = (flit >> (w * 64)) & ((1 << 64) - 1)
            self.setup.append((MAG_BASE + A_STAGE + (slot * FLIT_WORDS + w) * 8, word))
        return self

    # ---- program construction ------------------------------------------
    def wr(self, addr: int, data: int) -> "Program":
        data &= (1 << 64) - 1
        self.cmds.append(Command(OP_WR, addr, data))
        self._shadow[addr] = data
        return self

    def wr_setup(self, addr: int, data: int) -> "Program":
        """Write a LATCHED register, skipping it if it already holds this.

        Only for a register the hardware reads and never modifies. Two kinds
        are not eligible, and both fail silently rather than loudly:

        * one where the write itself is the event (PROG_KICK), and
        * one the hardware CONSUMES (PROG_CRED), where the shadow's value
          stops matching the real one the moment the machine runs.

        The test is not "does the driver want the same value again" -- it is
        "does the register still hold what the driver last wrote".

        This is where the command RAM is actually won. Dispatching a pass sets
        five registers, but across a round of passes only one or two of them
        differ, so most of those writes re-state what the register already
        holds. Dropping them roughly triples the passes a round can carry.
        """
        data &= (1 << 64) - 1
        if self._shadow.get(addr) == data:
            return self
        return self.wr(addr, data)

    def poll(self, addr: int, want: int, mask: int) -> "Program":
        self.cmds.append(Command(OP_POLL, addr, want, mask))
        return self

    def done(self, code: int) -> "Program":
        self.cmds.append(Command(OP_DONE, 0, code))
        return self

    def kick(self, x: int, y: int, base: int, nflits: int):
        """Launch a staged program at one CU. Does NOT wait for it.

        Kicking every cluster before waiting on any is the whole difference
        between clusters that overlap and clusters that take turns: a
        dispatch that polls to completion before the next one starts makes N
        independent clusters take N times as long as one.

        The dispatcher ignores a kick while it is still streaming the previous
        program, so this polls PROG_STAT first -- a dropped kick is silent, and
        the symptom is a cluster that simply never reports done.
        """
        idx = node_index(x, y)
        self.poll(MAG_BASE + A_PROG_STAT, 0, 0x1)  # wait for run == 0
        # DST / BASE / LEN are latched configuration: the dispatcher copies
        # them into its working counters on the kick and leaves the registers
        # alone, so re-writing an unchanged one is pure waste.
        self.wr_setup(MAG_BASE + A_PROG_DST, idx)
        self.wr_setup(MAG_BASE + A_PROG_BASE, base)
        self.wr_setup(MAG_BASE + A_PROG_LEN, nflits)
        # NOTE there is no PROG_CRED write here. Credit is seeded ONCE per
        # round by seed_credits; see the warning there for why re-seeding per
        # kick is not merely wasteful but wrong.
        self.wr(MAG_BASE + A_PROG_KICK, 1)  # the write IS the launch
        return self

    def seed_credits(self, n: int) -> "Program":
        """Set the dispatch credit for a whole round. Call once, before kicking.

        Credit is what keeps instructions in flight below the CU's instruction
        FIFO (``INST_DEPTH``, 32). That bound is not a performance tuning knob:
        a full FIFO raises ``noc_in_busy``, which backpressures the mesh link,
        which also blocks the MEMORY READ RESPONSES the CU is waiting on -- so
        it can never drain the FIFO it is being blocked by. The machine stops
        with no error, having executed nothing.

        So the counter must span the round, not a kick. Seeding it per kick
        lets P kicks admit P*n instructions against a FIFO of 32, and the wedge
        appears only once one CU receives more than 32 -- which is why it hides
        at small sizes and at wide cluster counts, and strikes when passes pile
        onto one cluster.

        One write per round also costs P-1 fewer commands than one per kick.
        """
        self.wr(MAG_BASE + A_PROG_CRED, n)
        return self

    def await_node(self, x: int, y: int, nflits: int) -> "Program":
        """Block until that CU's signal count EQUALS `nflits`.

        ONLY CORRECT ONCE PER POWER-UP. `node_status` in `noc_orchestrator.v`
        is written on every inbound CU_SIGNAL and is cleared by nothing -- not
        by writing SIG_DONE, which clears only the global counter, and not by
        `resetn`. So the second round asking for the same `nflits` waits for a
        count that has already gone past it, forever. Use
        :meth:`await_node_at` with a baseline read before the dispatch.
        """
        return self.await_node_at(x, y, nflits)

    def await_node_at(self, x: int, y: int, count: int) -> "Program":
        """Block until that CU's cumulative signal count reaches `count`.

        The count is cumulative and 16 bits, so `count` is
        ``(baseline + expected) & 0xFFFF`` for a baseline read from the same
        register before dispatching -- see `ktpu.hw.vector.run_kernel`.

        PREFER THIS TO :meth:`await_all` WHENEVER ANY OTHER NODE MIGHT SIGNAL.
        `A_SIG_DONE` counts completions from EVERY node, so in a run where a
        vector core is also reporting -- a kernel retiring, or a
        `SIG_DATA_RECEIVED` from a CU_DATA burst -- a wait sized for one
        dispatch is satisfied by somebody else's traffic. The dispatch is then
        released early and the next `upload` overwrites the staging RAM under a
        dispatcher still streaming out of it.
        """
        self.poll(node_status_addr(x, y), ((count & 0xFFFF) << 8) | 1, 0x00FF_FF01)
        return self

    def await_signal(self, x, y, code: int, count: int, arg=None) -> "Program":
        """Block until node (x,y)'s mirror shows `count` signals AND `code` last.

        `NODE_STATUS` is ``{code[8], arg[32], count[16], 7'd0, valid}``, and
        matching the CODE as well as the count is what separates this from
        :meth:`await_node`: a count alone is satisfied by any signal that node
        happens to emit, including a fault. `arg` narrows it further -- for
        `SIG_DATA_RECEIVED` the argument is the `buf_id` that landed.

        `count` is cumulative and never cleared, so it is a target computed
        from a read taken before the transfer, not a number of events.
        """
        want = ((code & 0xFF) << 56) | ((count & 0xFFFF) << 8) | 1
        mask = 0xFF00_0000_00FF_FF01
        if arg is not None:
            want |= (arg & MASK32) << 24
            mask |= 0x00FF_FFFF_FF00_0000
        self.poll(node_status_addr(x, y), want, mask)
        return self

    def clear_done(self) -> "Program":
        """Zero the global completion counter before dispatching."""
        self.wr(MAG_BASE + A_SIG_DONE, 0)
        return self

    def await_all(self, total_flits: int) -> "Program":
        """Block until `total_flits` completions have arrived from ANY node.

        ONE poll for the whole machine. Waiting per node costs a command per
        node, so the program grows with the cluster count for a question whose
        answer is a single number -- and the program is what the host has to
        push over AXI before anything runs.
        """
        self.poll(MAG_BASE + A_SIG_DONE, total_flits, 0xFFFF)
        return self

    def dispatch(self, x: int, y: int, nflits: int) -> "Program":
        """Kick one CU and wait for it. Sequential by construction."""
        self.seed_credits(INST_DEPTH)
        self.kick(x, y, 0, nflits)
        return self.await_node(x, y, nflits)

    # ---- shipping -------------------------------------------------------
    def upload(self, t: Transport) -> None:
        """Push the setup data -- instruction flits -- straight to the card.

        Coalesced on a transport with a bulk path, and only there: the block
        carries the same bytes to the same addresses either way, so this is a
        choice about how many transactions it costs and about nothing else.
        """
        if t.bulk:
            for addr, block in runs(self.setup):
                t.write_block(addr, block)
            return
        for addr, data in self.setup:
            t.write64(addr, data)

    def load(self, t: Transport) -> None:
        """Upload the setup data, then the control program."""
        self.upload(t)
        for n, c in enumerate(self.cmds):
            base = ORC_BASE + MO_CMD + n * 32
            for f, word in enumerate(c.words()):
                t.write64(base + f * 8, word)

    def go(self, t: Transport) -> None:
        t.write64(ORC_BASE + MO_CTRL, 1)

    def wait(self, t: Transport, limit: int = 1_000_000) -> int:
        """Poll CTRL until done; returns the DONE code."""
        for _ in range(limit):
            if t.read64(ORC_BASE + MO_CTRL) & 0x2:
                return t.read64(ORC_BASE + MO_CODE)
        raise TimeoutError("orchestrator did not finish")

    def execute(self, t: Transport, timeout: float = 30.0) -> int:
        """Run the control program ON THE HOST; returns the DONE code.

        `main_orch` is a bench module. A synthesised mesh wires the control
        window straight to the agent (mag.v s198), so on a card there is no
        command RAM to load and no GO to write -- the host IS the machine that
        executes WR, POLL and DONE. Same program and the same poll rule,
        `(read & mask) == want` unmasked, so a program that runs here runs on
        `main_orch` unchanged the day it is on the bitstream.

        Bounded in SECONDS rather than iterations, because an iteration is not
        a fixed cost: a poll is nanoseconds against a fake and ~100 us over
        PCIe, and one limit cannot mean the same thing to both.
        """
        self.upload(t)
        for pc, c in enumerate(self.cmds):
            if c.op == OP_WR:
                t.write64(c.addr, c.data)
            elif c.op == OP_POLL:
                self._poll(t, pc, c, timeout)
            elif c.op == OP_DONE:
                return c.data
            else:
                raise ValueError(f"command {pc}: unknown opcode {c.op}")
        raise ValueError(f"control program of {len(self.cmds)} ended without a DONE")

    @staticmethod
    def _poll(t: Transport, pc: int, c: "Command", timeout: float) -> None:
        """Spin on one register until it matches, or say what it held instead."""
        deadline = time.monotonic() + timeout
        last = t.read64(c.addr)
        while (last & c.mask) != c.data:
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"command {pc}: [{c.addr:#x}] & {c.mask:#018x} did not become "
                    f"{c.data:#018x} in {timeout:g}s; it holds {last:#018x}"
                )
            last = t.read64(c.addr)

    def __len__(self) -> int:
        return len(self.cmds)
