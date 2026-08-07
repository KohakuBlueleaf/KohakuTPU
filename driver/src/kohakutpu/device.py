"""Address map, control-program ISA, and the transport abstraction.

The transport is the whole point of this file. A driver written against
``write64`` / ``read64`` runs unchanged against a simulator, a JTAG-AXI master,
or XDMA -- only the backend differs. Everything above it is ordinary software.

See docs/mas/driver.md.
"""

import abc
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
A_PROG_DST = 0x0040
A_PROG_LEN = 0x0048
A_PROG_KICK = 0x0050
A_PROG_STAT = 0x0058  # R: [0] run, [16:1] flits left, [32:17] credit
A_PROG_CRED = 0x0060
A_PROG_BASE = 0x0068  # first staging slot of this program
A_SIG_DONE = 0x0070  # R: completions from every node; W: clear
A_NODE = 0x1000  # + {y,x}*8
A_STAGE = 0x2000  # instruction flits, 5 x 64-bit words each

# control-program opcodes
OP_WR = 1
OP_POLL = 2
OP_DONE = 3

FLIT_WORDS = 5  # 64-bit words per staged instruction flit

# noc_cu_base INST_DEPTH. Credit must never allow more instructions in flight
# than one CU's instruction FIFO can hold -- see Program.seed_credits.
INST_DEPTH = 32


def node_index(x: int, y: int) -> int:
    """NoC coordinate -> the {y,x} byte used by PROG_DST and NODE_STATUS."""
    return ((y & 0xF) << 4) | (x & 0xF)


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------
class Transport(abc.ABC):
    """A 64-bit AXI window. Two methods is the entire hardware dependency."""

    @abc.abstractmethod
    def write64(self, addr: int, data: int) -> None: ...

    @abc.abstractmethod
    def read64(self, addr: int) -> int: ...


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
        """Block until that CU has reported `nflits` completion signals."""
        idx = node_index(x, y)
        # NODE_STATUS: [0] valid, [23:8] signals
        self.poll(MAG_BASE + A_NODE + idx * 8, (nflits << 8) | 1, 0x00FF_FF01)
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
        """Push the setup data -- instruction flits -- straight to the card."""
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

    def __len__(self) -> int:
        return len(self.cmds)
