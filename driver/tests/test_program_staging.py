"""Staging must never leave `Program.upload` one 64-bit word at a time.

The failure has NO SYMPTOM on hardware -- on a pre-2026-08-23 bitstream a word
write covers a whole 32-byte fabric window and zeroes the three staging words
sharing it, so only the last of every four survives and nothing reports an
error. `docs/spec/control-registers.md` §2.7. Only a test holds this.
"""

import pytest
from kohakuaccel.device.program import Program
from kohakuaccel.device.registers import A_STAGE, FLIT_WORDS, MAG_BASE
from kohakuaccel.transport.base import Transport


class Bulk(Transport):
    """What every transport that reaches silicon looks like."""

    bulk = True

    def __init__(self) -> None:
        self.blocks: list[tuple[int, bytes]] = []
        self.words: list[tuple[int, int]] = []

    def write64(self, addr: int, data: int) -> None:
        self.words.append((addr, data))

    def write_block(self, addr: int, data: bytes) -> None:
        self.blocks.append((addr, data))

    def read64(self, addr: int) -> int:
        return 0


class NoBulk(Bulk):
    """A backend that forgot to set `bulk` -- the dangerous default."""

    bulk = False


def staged() -> Program:
    return Program().stage_flit(0, 0x1111).stage_flit(1, 0x2222)


def test_staging_is_refused_without_a_bulk_write_path():
    with pytest.raises(RuntimeError, match="§2.7"):
        staged().upload(NoBulk())


def test_the_refusal_names_the_silent_loss():
    with pytest.raises(RuntimeError, match="last of every four"):
        staged().upload(NoBulk())


def test_staging_goes_out_as_blocks_and_never_word_by_word():
    t = Bulk()
    staged().upload(t)
    assert t.words == [], "staging must not reach write64"
    assert len(t.blocks) == 1, "consecutive slots are one contiguous block"
    addr, data = t.blocks[0]
    assert addr == MAG_BASE + A_STAGE
    assert len(data) == 2 * FLIT_WORDS * 8


def test_a_program_with_no_staging_still_needs_no_bulk_path():
    """The refusal is about STAGE, not about uploading at all."""
    Program().upload(NoBulk())


def test_slots_are_emitted_in_ascending_order():
    """The trailing partial window zeroes the words ABOVE a run.

    That is harmless only because slots ascend, so the zeros land on a slot
    written later. Staging downwards would zero a live one.
    """
    p = Program().stage_flit(2, 0xAAAA).stage_flit(3, 0xBBBB)
    addrs = [a for a, _ in p.setup]
    assert addrs == sorted(addrs)


def test_a_slot_is_forty_bytes_so_four_of_them_close_a_window():
    """Why flit alignment cannot be guaranteed: 40 and 32 agree every 4 slots."""
    assert FLIT_WORDS * 8 == 40
    assert (4 * FLIT_WORDS * 8) % 32 == 0
    assert any((n * FLIT_WORDS * 8) % 32 for n in (1, 2, 3))
