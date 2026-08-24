"""The machine's control program: dispatch, wait, halt.

Built against the control-program ISA the main orchestrator executes, and
runnable on the host against the same registers -- a synthesised mesh wires the
control window straight to the agent, so there is no command RAM on a card and
the host is the machine that executes WR, POLL and DONE.
"""

import time
from dataclasses import dataclass, field

from kohakuaccel.device.registers import (
    A_PROG_BASE,
    A_PROG_CRED,
    A_PROG_DST,
    A_PROG_KICK,
    A_PROG_LEN,
    A_PROG_STAT,
    A_SIG_DONE,
    A_STAGE,
    FLIT_WORDS,
    INST_DEPTH,
    MAG_BASE,
    MO_CMD,
    MO_CODE,
    MO_CTRL,
    OP_DONE,
    OP_POLL,
    OP_WR,
    ORC_BASE,
    node_index,
    node_status_addr,
)
from kohakuaccel.transport.base import MASK32, MASK64, Transport, runs


@dataclass
class Command:
    """One control-program instruction: write, poll, or halt."""

    op: int
    addr: int = 0
    data: int = 0
    mask: int = 0

    def words(self) -> tuple[int, int, int, int]:
        return (self.op, self.addr, self.data, self.mask)


@dataclass
class Program:
    """A control program plus the data the host uploads before it runs.

    THE PROGRAM IS NOT A RECORDING OF EVERY AXI WRITE. Instruction flits and
    operand data are uploaded into the card's memory before GO; putting them in
    the command RAM makes the host ship each flit twice and makes the program
    grow with the problem rather than with its control flow. So `setup` collects
    writes the host performs directly and `cmds` holds only control.
    """

    cmds: list[Command] = field(default_factory=list)
    setup: list[tuple[int, int]] = field(default_factory=list)
    # What this program has already written to each register. Starts empty, so a
    # program never assumes a value it did not set itself.
    _shadow: dict[int, int] = field(default_factory=dict)
    # What `seed_credits` asked for; `kick` restates it. See `seed_credits`.
    _credit: int = 0

    def stage_flit(self, slot: int, flit: int) -> "Program":
        """Upload one instruction flit into the staging RAM as direct writes.

        The staging RAM is already AXI addressable, so routing this through the
        command RAM would only add a copy.

        IT IS WRITE-ONLY. `noc_orchestrator`'s read decode has no A_STAGE case,
        so a readback is 0 BY DESIGN and is not evidence of a failed write; only
        PROG_KICK proves the contents. Do not "verify" an upload by reading it.

        A SLOT IS 40 BYTES AND THE FABRIC'S WRITE WINDOW IS 32, so a run of `n`
        slots ends on a window boundary only when `n % 4 == 0` and flit-aligned
        staging cannot be guaranteed -- write-only means no read-modify-write to
        pad with. On a pre-2026-08-23 bitstream the trailing partial window
        ZEROES up to three words above the run, which land in the NEXT slot.
        Harmless while slots ascend, as they do here. It is NOT harmless to
        stage a lower slot range while a higher one is live -- which is the
        multi-program case `PROG_BASE` exists for. `control-registers.md` §2.7.
        """
        for w in range(FLIT_WORDS):
            word = (flit >> (w * 64)) & MASK64
            self.setup.append((MAG_BASE + A_STAGE + (slot * FLIT_WORDS + w) * 8, word))
        return self

    def wr(self, addr: int, data: int) -> "Program":
        """Append an unconditional 64-bit write."""
        data &= MASK64
        self.cmds.append(Command(OP_WR, addr, data))
        self._shadow[addr] = data
        return self

    def wr_setup(self, addr: int, data: int) -> "Program":
        """Write a LATCHED register, skipping it if it already holds this.

        Only for a register the hardware reads and never modifies. Two kinds are
        not eligible and both fail silently: one where the write itself is the
        event (PROG_KICK), and one the hardware CONSUMES (PROG_CRED), where the
        shadow stops matching the moment the machine runs.

        Dispatching a pass sets five registers, but across a round only one or
        two differ, so dropping the restatements roughly triples the passes a
        round can carry.
        """
        data &= MASK64
        if self._shadow.get(addr) == data:
            return self
        return self.wr(addr, data)

    def poll(self, addr: int, want: int, mask: int) -> "Program":
        """Append a poll that blocks until ``(read & mask) == want``."""
        self.cmds.append(Command(OP_POLL, addr, want, mask))
        return self

    def done(self, code: int) -> "Program":
        """Append the halt that returns `code`."""
        self.cmds.append(Command(OP_DONE, 0, code))
        return self

    def kick(self, x: int, y: int, base: int, nflits: int) -> "Program":
        """Launch a staged program at one unit. Does NOT wait for it.

        Kicking every unit before waiting on any is the difference between units
        that overlap and units that take turns. The dispatcher ignores a kick
        while still streaming the previous program, so this polls PROG_STAT
        first -- a dropped kick is silent, and the symptom is a unit that simply
        never reports done.
        """
        idx = node_index(x, y)
        self.poll(MAG_BASE + A_PROG_STAT, 0, 0x1)  # wait for run == 0
        # ORDER IS LOAD-BEARING, AND IT COMPENSATES FOR A DEFECT rather than for
        # machine behaviour -- docs/spec/control-registers.md s2.7. Before the
        # 2026-08-23 fix one 64-bit write covers a 32-byte flit and writes FOUR
        # registers, so PROG_LEN is what launches the dispatch and PROG_KICK
        # below launches nothing; any other order dispatches to node {0,0} and
        # is dropped silently. v7 and v7.1 do not carry the fix, so KEEP THIS --
        # retire it only when a post-fix bitstream ships, deliberately.
        if self._shadow.get(MAG_BASE + A_PROG_BASE) != (base & MASK64):
            self._shadow.pop(MAG_BASE + A_PROG_LEN, None)
            self._shadow.pop(MAG_BASE + A_PROG_DST, None)
        self.wr_setup(MAG_BASE + A_PROG_BASE, base)
        self.wr_setup(MAG_BASE + A_PROG_LEN, nflits)
        self.wr_setup(MAG_BASE + A_PROG_DST, idx)
        if self._credit:
            self.wr(MAG_BASE + A_PROG_CRED, self._credit)
        self.wr(MAG_BASE + A_PROG_KICK, 1)  # the write IS the launch
        return self

    def seed_credits(self, n: int) -> "Program":
        """Set the dispatch credit for a whole round. Call once, before kicking.

        Credit keeps instructions in flight below the unit's instruction FIFO
        (:data:`INST_DEPTH`). That bound is not a tuning knob: a full FIFO raises
        ``noc_in_busy``, which backpressures the link, which also blocks the
        MEMORY READ RESPONSES the unit is waiting on -- so it can never drain the
        FIFO it is blocked by, and the machine stops with no error having
        executed nothing.

        RECORDS the value; `kick` is what writes it, because on pre-2026-08-23
        silicon the `PROG_BASE` write zeroes the credit counter -- a defect, not
        machine behaviour (`docs/spec/control-registers.md` §2.7) -- and
        `PROG_BASE` is written per kick. The write is a LOAD and not an add, so
        restating it per kick admits n per kick rather than P*n across P of them.
        """
        self._credit = n
        self.wr(MAG_BASE + A_PROG_CRED, n)
        return self

    def await_node_at(self, x: int, y: int, count: int) -> "Program":
        """Block until that unit's cumulative signal count reaches `count`.

        The count is cumulative and 16 bits, so `count` is
        ``(baseline + expected) & 0xFFFF`` for a baseline read taken before
        dispatching.

        PREFER THIS TO :meth:`await_all` WHENEVER ANY OTHER NODE MIGHT SIGNAL.
        A_SIG_DONE counts completions from EVERY node, so a wait sized for one
        dispatch is satisfied by somebody else's traffic, releasing the dispatch
        early and letting the next upload overwrite the staging RAM under a
        dispatcher still streaming out of it.

        DO NOT "OPTIMISE" THIS INTO A SINGLE A_SIG_DONE POLL. It works on a
        pre-2026-08-23 bitstream only because NODE_STATUS sits at 0x1000+, clear
        of the 32-byte flit holding SIG_DONE -- an accident of address, not a
        design. Every PROG_CRED and PROG_BASE write clears SIG_DONE, so a kick
        clears it twice. `docs/spec/control-registers.md` §2.7.
        """
        self.poll(node_status_addr(x, y), ((count & 0xFFFF) << 8) | 1, 0x00FF_FF01)
        return self

    def await_signal(self, x, y, code: int, count: int, arg=None) -> "Program":
        """Block until node (x,y)'s mirror shows `count` signals AND `code` last.

        NODE_STATUS is ``{code[8], arg[32], count[16], 7'd0, valid}``. Matching
        the CODE as well as the count is what separates this from
        :meth:`await_node_at`: a count alone is satisfied by any signal that node
        emits, including a fault. `arg` narrows it further -- for
        SIG_DATA_RECEIVED the argument is the `buf_id` that landed.
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

        One poll for the whole machine. Waiting per node costs a command per
        node, so the program would grow with the unit count for a question whose
        answer is a single number.

        DO NOT USE ON A PRE-2026-08-23 BITSTREAM -- it hangs, and this method has
        no callers for that reason. Every `PROG_CRED` and `PROG_BASE` write also
        clears A_SIG_DONE (0x70 shares their 32-byte flit), so each `kick` clears
        it twice and every kick in a round destroys the completions the previous
        ones counted. `await_node_at` is immune only because NODE_STATUS lives at
        0x1000+, outside those flits. See docs/spec/control-registers.md s2.7.
        """
        self.poll(MAG_BASE + A_SIG_DONE, total_flits, 0xFFFF)
        return self

    def dispatch(self, x: int, y: int, nflits: int) -> "Program":
        """Kick one unit and wait for it. Sequential by construction."""
        self.seed_credits(INST_DEPTH)
        self.kick(x, y, 0, nflits)
        return self.await_node_at(x, y, nflits)

    def upload(self, t: Transport) -> None:
        """Push the setup data -- instruction flits -- straight to the card.

        ALWAYS coalesced, and STAGING IS REFUSED on a transport without a bulk
        path. Word by word, staging is silently destructive on a pre-2026-08-23
        bitstream: one 64-bit write covers a whole 32-byte flit and zeroes the
        three words sharing it, and since the addresses ascend only the LAST
        word of every four survives -- three quarters of every instruction gone,
        with no error anywhere. `docs/spec/control-registers.md` §2.7.

        `Transport.bulk` defaults to FALSE, so a backend that forgets to set it
        lands on the destructive path; that is why this refuses rather than
        warns. Every transport that reaches silicon sets it.
        """
        blocks = runs(self.setup)
        stage = MAG_BASE + A_STAGE
        if not t.bulk and any(addr >= stage for addr, _ in blocks):
            raise RuntimeError(
                f"{type(t).__name__} has no bulk write path, so staging at "
                f"{stage:#x}+ would go one 64-bit word at a time. On a "
                f"pre-2026-08-23 bitstream that ZEROES the three staging words "
                f"sharing each 32-byte flit and only the last of every four "
                f"survives -- silently. Give the transport a real write_block "
                f"and set bulk = True. See docs/spec/control-registers.md §2.7"
            )
        for addr, block in blocks:
            t.write_block(addr, block)

    def load(self, t: Transport) -> None:
        """Upload the setup data, then the control program into the command RAM."""
        self.upload(t)
        for n, c in enumerate(self.cmds):
            base = ORC_BASE + MO_CMD + n * 32
            for f, word in enumerate(c.words()):
                t.write64(base + f * 8, word)

    def go(self, t: Transport) -> None:
        """Assert GO on the main orchestrator."""
        t.write64(ORC_BASE + MO_CTRL, 1)

    def wait(self, t: Transport, limit: int = 1_000_000) -> int:
        """Poll CTRL until done and return the DONE code.

        Raises :class:`TimeoutError` after `limit` reads.
        """
        for _ in range(limit):
            if t.read64(ORC_BASE + MO_CTRL) & 0x2:
                return t.read64(ORC_BASE + MO_CODE)
        raise TimeoutError("orchestrator did not finish")

    def execute(self, t: Transport, timeout: float = 30.0) -> int:
        """Run the control program ON THE HOST and return the DONE code.

        Same program and the same poll rule as the command-RAM path, so a
        program that runs here runs on the orchestrator unchanged.

        Bounded in SECONDS rather than iterations: a poll is nanoseconds against
        an in-process model and ~100 us over PCIe, and one iteration limit
        cannot mean the same thing to both.
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
    def _poll(t: Transport, pc: int, c: Command, timeout: float) -> None:
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
