"""Boards: the topology and the address map as data the driver is handed.

The test that matters most is the negative one -- that a board which cannot
reach mag.v's quantise marker REFUSES a pre-quantised upload. FP16 landing in
a region sized for MXFP7 overruns the operand after it and the run answers
without an error, which is the one failure no later check catches.
"""

import json

import pytest

from ktpu.hw import bench
from ktpu.hw import board as bd
from ktpu.hw import device as dev


def test_the_shipped_board_matches_the_synthesised_bitstream():
    """Coordinates from the mesh IP's own synthesis log; see the file's `source`."""
    b = bd.Board.named("singlemesh_2x2")
    assert b.layout == "split" and b.ncl == 2
    assert b.clusters == ((1, 1), (1, 2))
    assert b.accumulators == ((2, 1), (2, 2))
    assert b.ports == ((0, 1), (0, 2))
    assert b.ctrl_base == 0x4_0080_0000
    assert b.orchestrator == "host", "no synth_top mesh instantiates main_orch"
    assert (b.tiles, b.l1_a, b.l1_b) == (256, 32, 32), "mx_cluster_node's parameters"
    assert b.source, "a board that cannot say where its numbers came from is a guess"


def test_the_shipped_boards_COORDINATES_agree_with_bench_mesh():
    """Two independent descriptions of one machine, checked against each other.

    The board's coordinates come from the bitstream's synthesis log and
    `bench.mesh`'s from the generator; they disagreed until `_split` was
    corrected, and a flit to a wrong node is a hang rather than an error.
    """
    why = bd.Board.named("singlemesh_2x2").verify()
    assert not [
        w for w in why if "coordinates" in w or "ports" in w or "grid" in w
    ], why


def test_the_shipped_boards_CAPACITIES_differ_and_verify_says_so():
    """The bitstream is frozen at TILES=256/32/32; the RTL has moved to 512/128/256.

    This is not a defect in either -- it is what `verify` exists to report. It
    is also the difference that put 15,440 of 16,384 elements past 10% error on
    real silicon with every gating check passing, so it must never be silent.
    """
    why = bd.Board.named("singlemesh_2x2").verify()
    assert any("TILES: board 256" in w for w in why), why
    assert any("L1 A entries: board 32" in w for w in why), why
    assert any("L1 B entries: board 32" in w for w in why), why


def test_a_bench_board_agrees_with_bench_mesh():
    assert bd.bench_board(2, "merged").verify() == []
    assert bd.bench_board(4, "merged").verify() == []


# ---- the address map ----------------------------------------------------
def test_control_registers_land_in_the_ctrl_window():
    b = bd.Board.named("singlemesh_2x2")
    assert b.ctrl(dev.A_PROG_KICK) == 0x4_0080_0050
    assert b.rebase(dev.MAG_BASE + dev.A_PROG_KICK) == 0x4_0080_0050
    assert b.rebase(dev.MAG_BASE + dev.A_STAGE) == 0x4_0080_2000


def test_a_main_orch_address_is_refused_on_a_board_that_has_none():
    b = bd.Board.named("singlemesh_2x2")
    with pytest.raises(ValueError, match="no main_orch"):
        b.rebase(dev.ORC_BASE + dev.MO_CTRL)


def test_the_bench_board_keeps_both_halves():
    b = bd.bench_board(2)
    assert b.rebase(dev.ORC_BASE + dev.MO_CTRL) == dev.ORC_BASE + dev.MO_CTRL
    assert b.rebase(dev.MAG_BASE + dev.A_STAGE) == dev.MAG_BASE + dev.A_STAGE


def test_memory_words_are_256_bits_wide():
    """bench.plan counts in 256-bit words; a byte address is 32 times one."""
    assert bd.Board.named("singlemesh_2x2").mem_byte(3) == 96


def test_a_block_may_not_straddle_the_orc_mag_boundary():
    b = bd.bench_board(2)
    with pytest.raises(ValueError, match="crosses"):
        b.one_window(dev.MAG_BASE - 8, 32)


# ---- the quantise marker ------------------------------------------------
def test_an_unmarked_upload_is_just_an_address():
    b = bd.Board.named("singlemesh_2x2")
    assert b.upload_addr(2) == 64
    assert b.upload_addr(2, quant=False) == 64


def test_a_board_that_cannot_mark_refuses_to_pretend():
    b = bd.Board.named("singlemesh_2x2")
    with pytest.raises(ValueError, match="widen the S_AXI_MEM segment"):
        b.upload_addr(0, quant=True)


def test_a_board_that_can_mark_sets_the_bits_mag_reads():
    """mag.v s565: HW_QUANT is bit 33 and HW_BLAY bit 32 of the 34-bit address."""
    b = bd.bench_board(2)
    assert b.upload_addr(0, quant=True) == 1 << 33
    assert b.upload_addr(0, quant=True, blayout=1) == (1 << 33) | (1 << 32)
    assert b.upload_addr(0, quant=False, blayout=1) == 0


# ---- files --------------------------------------------------------------
def test_a_board_round_trips_through_json(tmp_path):
    b = bd.bench_board(4, "merged")
    again = bd.Board.load(b.save(tmp_path / "b.json"))
    assert again == b


def test_addresses_are_written_as_hex(tmp_path):
    """A 34-bit map in decimal is unreadable, and unreadable is unreviewable."""
    d = json.loads((bd.bench_board(2).save(tmp_path / "b.json")).read_text())
    assert d["ctrl_base"] == "0x10000000"


def test_an_unknown_board_names_the_ones_there_are():
    with pytest.raises(FileNotFoundError, match="singlemesh_2x2"):
        bd.Board.named("no_such_board")


def test_a_bad_layout_is_refused():
    with pytest.raises(ValueError, match="unknown layout"):
        bd.Board(
            name="x", layout="mergd", clusters=[], ports=[], ncol=1, nrow=1, ctrl_base=0
        )


# ---- the rebased view ---------------------------------------------------
def test_a_program_reaches_the_board_addresses(monkeypatch):
    """A planned round, executed through a board, lands in the ctrl window."""
    b = bd.Board.named("singlemesh_2x2")
    inner = dev.MemoryTransport()
    prog = bench.build(16, 32, 16, ncl=2).programs[0]
    bench_sig = dev.MAG_BASE + dev.A_SIG_DONE
    want = next(
        c.data for c in prog.cmds if c.op == dev.OP_POLL and c.addr == bench_sig
    )
    inner.on_read[b.ctrl(dev.A_SIG_DONE)] = lambda n, v=want: v

    prog.execute(b.view(inner), timeout=5.0)

    assert inner.mem[b.ctrl(dev.A_PROG_KICK)] == 1
    assert all(0x4_0080_0000 <= a < 0x4_0081_0000 for a in inner.mem)


def test_the_view_keeps_the_inner_bulk_flag():
    b = bd.bench_board(2)
    assert b.view(dev.MemoryTransport()).bulk is True
    assert b.view(dev.RecordingTransport()).bulk is False


def test_a_rebased_block_is_the_same_bytes_at_the_moved_address():
    b = bd.Board.named("singlemesh_2x2")
    inner = dev.MemoryTransport()
    payload = bytes(range(24))
    b.view(inner).write_block(dev.MAG_BASE + dev.A_STAGE, payload)
    assert inner.read_block(0x4_0080_2000, 24) == payload


# ---- backend selection --------------------------------------------------
def test_open_transport_names_what_it_does_not_know():
    with pytest.raises(dev.TransportUnavailable, match="unknown transport"):
        bd.open_transport("usb")


def test_open_transport_reads_the_environment(monkeypatch):
    monkeypatch.setenv("KTPU_TRANSPORT", "memory")
    assert isinstance(bd.open_transport(), dev.MemoryTransport)


def test_open_transport_falls_back_to_the_boards_own_choice(monkeypatch):
    monkeypatch.delenv("KTPU_TRANSPORT", raising=False)
    t = bd.open_transport(board=bd.bench_board(2))
    assert isinstance(t.inner, dev.RecordingTransport)


def test_an_absent_card_is_unavailable_not_an_oserror(monkeypatch):
    """The requirement in the brief: catchable, and it explains itself."""
    monkeypatch.setattr("ktpu.hw.xdma._api", None)
    monkeypatch.setattr("ktpu.hw.xdma.sys.platform", "linux")
    with pytest.raises(dev.TransportUnavailable):
        bd.open_transport("xdma")
