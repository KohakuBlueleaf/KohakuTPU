"""The transport contract: a block is N word accesses and nothing else.

That equivalence is the whole reason the bulk path can be added without a
second contract to keep in step. Every test here is a way of asking it: the
same program pushed through a word-only backend and a block-capable one has to
leave the same bytes at the same addresses.

`Program.execute` is checked here too, because on a synthesised mesh the host
IS the orchestrator -- `main_orch` is a bench module -- so this loop is the
control machine on real hardware and has no RTL bench of its own.
"""

import pytest

from ktpu.hw import bench
from ktpu.hw import device as dev


class WordOnly(dev.Transport):
    """The default block path: whatever the base class builds out of words."""

    def __init__(self):
        self.mem = {}
        self.order = []

    def write64(self, addr, data):
        self.order.append(addr)
        self.mem[addr] = data & dev.MASK64

    def read64(self, addr):
        return self.mem.get(addr, 0)


def test_write_block_is_word_writes():
    t = WordOnly()
    t.write_block(0x40, bytes(range(24)))
    assert t.order == [0x40, 0x48, 0x50]
    assert t.mem[0x40] == int.from_bytes(bytes(range(8)), "little")
    assert t.mem[0x50] == int.from_bytes(bytes(range(16, 24)), "little")


def test_read_block_round_trips_write_block():
    t = WordOnly()
    payload = bytes((i * 7 + 3) & 0xFF for i in range(80))
    t.write_block(0x2000, payload)
    assert t.read_block(0x2000, len(payload)) == payload


def test_block_matches_the_equivalent_word_loop():
    words = [0x0011_2233_4455_6677, 0xFFFF_FFFF_FFFF_FFFF, 0]
    loop, block = WordOnly(), dev.MemoryTransport()
    for i, w in enumerate(words):
        loop.write64(0x100 + i * 8, w)
    block.write_block(0x100, b"".join(w.to_bytes(8, "little") for w in words))
    assert loop.mem == block.mem


@pytest.mark.parametrize("nbytes", [1, 7, 9, 15])
def test_partial_words_are_refused(nbytes):
    t = WordOnly()
    with pytest.raises(ValueError, match="whole number"):
        t.write_block(0, bytes(nbytes))
    with pytest.raises(ValueError, match="whole number"):
        t.read_block(0, nbytes)


def test_unaligned_is_refused():
    with pytest.raises(ValueError, match="aligned"):
        dev.MemoryTransport().write64(0x4, 1)


# ---- runs ---------------------------------------------------------------
def test_runs_groups_contiguous_and_breaks_on_a_gap():
    pairs = [(0x00, 1), (0x08, 2), (0x10, 3), (0x40, 4), (0x48, 5)]
    got = dev.runs(pairs)
    assert [a for a, _ in got] == [0x00, 0x40]
    assert [len(b) for _, b in got] == [24, 16]


def test_runs_does_not_reorder_a_descending_pair():
    """Descending is a break, not a sort: replay order has to be preserved."""
    assert [a for a, _ in dev.runs([(0x10, 1), (0x08, 2)])] == [0x10, 0x08]


def test_runs_is_little_endian_like_write64():
    assert dev.runs([(0, 0x0102_0304_0506_0708)])[0][1] == bytes(
        [8, 7, 6, 5, 4, 3, 2, 1]
    )


def test_runs_masks_to_64_bits():
    assert dev.runs([(0, (1 << 64) + 5)])[0][1] == (5).to_bytes(8, "little")


# ---- upload -------------------------------------------------------------
def _staged():
    """Four 288-bit flits, every byte distinct, so a misplaced word shows."""
    p = dev.Program()
    for slot in range(4):
        body = bytes((slot * 36 + i + 1) & 0xFF for i in range(36))
        p.stage_flit(slot, int.from_bytes(body, "little"))
    return p


class Bulky(dev.Transport):
    """A backend whose blocks are real, recording each as ONE transaction."""

    bulk = True

    def __init__(self):
        self.blocks = []

    def write64(self, addr, data):  # pragma: no cover - bulk path is used
        self.write_block(addr, (data & dev.MASK64).to_bytes(8, "little"))

    def read64(self, addr):  # pragma: no cover - never read
        raise NotImplementedError

    def write_block(self, addr, data):
        self.blocks.append((addr, bytes(data)))


def test_a_bulk_backend_and_the_recorder_produce_the_same_transactions():
    """THE INVARIANT the bulk path rests on, asserted directly.

    Not "the same memory afterwards" -- the same WRITES: expanding the blocks
    one backend issued has to give, address for address and word for word, the
    list the other recorded. A block that reordered, padded or coalesced across
    a gap would still leave plausible memory and would fail here.
    """
    prog = _staged().wr(0x58, 3).wr(0x60, 4)
    rec, bulk = dev.RecordingTransport(), Bulky()
    prog.upload(rec)
    prog.upload(bulk)

    flat = [
        (addr + off, int.from_bytes(block[off : off + 8], "little"))
        for addr, block in bulk.blocks
        for off in range(0, len(block), 8)
    ]
    assert flat == rec.writes


def test_upload_leaves_the_same_memory_bulk_or_not():
    p = _staged()
    word, block = WordOnly(), dev.MemoryTransport()
    assert word.bulk is False and block.bulk is True
    p.upload(word)
    p.upload(block)
    assert word.mem == block.mem


def test_upload_coalesces_a_staging_window_into_one_block():
    """20 words, one descriptor. This is the whole argument for the bulk path."""
    t = dev.MemoryTransport()
    _staged().upload(t)
    assert t.blocks == [(dev.MAG_BASE + dev.A_STAGE, 4 * dev.FLIT_WORDS * 8)]


def test_upload_of_a_real_round_is_a_handful_of_blocks():
    prob = bench.build(16, 32, 16, ncl=2)
    for prog in prob.programs:
        blocks = dev.runs(prog.setup)
        assert len(blocks) <= 2, [hex(a) for a, _ in blocks]
        assert sum(len(b) for _, b in blocks) == len(prog.setup) * 8


# ---- execute ------------------------------------------------------------
def test_execute_performs_writes_and_returns_the_done_code():
    t = dev.MemoryTransport()
    p = dev.Program().wr(0x80, 0xABC).wr(0x88, 0xDEF).done(0xC0DE)
    assert p.execute(t) == 0xC0DE
    assert t.mem[0x80] == 0xABC and t.mem[0x88] == 0xDEF


def test_execute_spins_until_the_poll_matches():
    """Third read matches, so the loop must read three times and not give up."""
    addr = dev.MAG_BASE + dev.A_SIG_DONE
    t = dev.MemoryTransport(on_read={addr: lambda n: 0 if n < 3 else 4})
    dev.Program().await_all(4).done(1).execute(t)
    assert t.reads[addr] == 3


def test_execute_masks_the_read_but_not_the_wanted_value():
    """main_orch s281 compares (read & mask) == want, with want unmasked."""
    t = dev.MemoryTransport(on_read={0x100: lambda n: 0xFFFF_FFFF_FFFF_FF01})
    dev.Program().poll(0x100, 0x01, 0xFF).done(0).execute(t, timeout=1.0)
    with pytest.raises(TimeoutError):
        dev.Program().poll(0x100, 0xFF01, 0xFF).done(0).execute(t, timeout=0.05)


def test_execute_names_the_register_that_never_matched():
    t = dev.MemoryTransport()
    p = dev.Program().poll(0x1234_5678, 0xFF, 0xFF).done(0)
    with pytest.raises(TimeoutError, match="0x12345678"):
        p.execute(t, timeout=0.05)


def test_execute_uploads_the_flits_before_running():
    t = dev.MemoryTransport()
    p = _staged().done(7)
    assert p.execute(t) == 7
    assert t.mem[dev.MAG_BASE + dev.A_STAGE] != 0


def test_execute_refuses_a_program_with_no_done():
    with pytest.raises(ValueError, match="without a DONE"):
        dev.Program().wr(0x8, 1).execute(dev.MemoryTransport())


def test_execute_refuses_an_unknown_opcode():
    p = dev.Program()
    p.cmds.append(dev.Command(op=9))
    with pytest.raises(ValueError, match="unknown opcode 9"):
        p.execute(dev.MemoryTransport())


def test_execute_runs_a_planned_round_against_a_fake_machine():
    """The planner's own program, end to end, with the machine faked as ready.

    PROG_STAT reads 0 (never running) and SIG_DONE answers whatever the round
    is waiting for, so every poll is satisfiable and the program has to retire
    on its own commands rather than on a timeout.
    """
    prob = bench.build(16, 32, 16, ncl=2)
    prog = prob.programs[0]
    sig = dev.MAG_BASE + dev.A_SIG_DONE
    flits = [c.data for c in prog.cmds if c.op == dev.OP_POLL and c.addr == sig]
    assert len(flits) == 1, "control() waits on SIG_DONE exactly once per round"
    t = dev.MemoryTransport(on_read={sig: lambda n, v=flits[0]: v})
    assert prog.execute(t, timeout=5.0) == 0xC0DE
    assert t.mem[dev.MAG_BASE + dev.A_PROG_KICK] == 1
