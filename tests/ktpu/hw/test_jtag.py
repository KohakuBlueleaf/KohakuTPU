"""The JTAG backend against a fake Tcl session, with no Vivado anywhere.

The Tcl is the interface, so the Tcl is what is checked: that a beat is
sixteen hex characters in ADDRESS ORDER, that a read parses back to the same
words, and that the size guard fires before someone waits two minutes for an
operand upload over a 0.08 MB/s link.
"""

import pytest

from ktpu.hw import device as dev
from ktpu.hw import jtag


class FakeSession:
    """Enough Tcl to answer this driver: a word store and two procs."""

    def __init__(self, width=8):
        self.mem = {}
        self.width = width
        self.scripts = []

    def __call__(self, script, host=None, port=None, timeout=None):
        self.scripts.append(script)
        if "bytes_per_beat" in script:
            return f"{self.width}\n"
        if "require_connected" in script:
            return ""
        head, rest = script.split(None, 1)
        if head == "jaxi::write":
            addr, beats = rest.split(None, 1)
            for i, b in enumerate(beats.strip("{}").split()):
                assert len(b) == 16, f"beat {b!r} is not a 64-bit hex word"
                self.mem[int(addr) + i * 8] = int(b, 16)
            return ""
        assert head == "jaxi::read", script
        addr, n = (int(v) for v in rest.split())
        return " ".join(f"{self.mem.get(addr + i * 8, 0):016x}" for i in range(n))


@pytest.fixture
def session(monkeypatch):
    s = FakeSession()
    monkeypatch.setattr(jtag, "evaluate", s)
    return s


def test_a_word_round_trips(session):
    t = jtag.JtagTransport(tcl="x.tcl")
    t.write64(0x40, 0x0011_2233_4455_6677)
    assert t.read64(0x40) == 0x0011_2233_4455_6677


def test_a_block_is_beats_in_address_order(session):
    t = jtag.JtagTransport(tcl="x.tcl")
    payload = bytes(range(24))
    t.write_block(0x100, payload)
    assert session.mem[0x100] == int.from_bytes(payload[:8], "little")
    assert session.mem[0x110] == int.from_bytes(payload[16:], "little")
    assert t.read_block(0x100, 24) == payload


def test_a_block_is_one_tcl_round_trip(session):
    """Three beats, one call. Per-word calls are what make JTAG unusable."""
    t = jtag.JtagTransport(tcl="x.tcl")
    before = t.calls
    t.write_block(0, bytes(24))
    assert t.calls == before + 1


def test_the_base_is_added_to_every_address(session):
    jtag.JtagTransport(base=0x4_0080_0000, tcl="x.tcl").write64(0x58, 1)
    assert 0x4_0080_0058 in session.mem


def test_an_operand_sized_block_is_refused_with_the_time_it_would_cost(session):
    t = jtag.JtagTransport(tcl="x.tcl")
    with pytest.raises(ValueError, match="control-plane"):
        t.write_block(0, bytes(8 << 20))


def test_the_limit_can_be_raised_deliberately(session):
    jtag.JtagTransport(tcl="x.tcl", max_block=1 << 20).write_block(0, bytes(1 << 20))
    assert len(session.mem) == (1 << 20) // 8


def test_a_narrow_master_is_refused(monkeypatch):
    """A 32-bit master would put every register write in the wrong half."""
    monkeypatch.setattr(jtag, "evaluate", FakeSession(width=4))
    with pytest.raises(dev.TransportUnavailable, match="32 bits wide"):
        jtag.JtagTransport(tcl="x.tcl")


def test_no_server_is_unavailable_not_an_oserror():
    with pytest.raises(dev.TransportUnavailable, match="Vivado Tcl server"):
        jtag.evaluate("puts hi", port=1)


def test_it_sources_the_helpers_only_when_given_a_path(session):
    jtag.JtagTransport(tcl="C:/hw/tools/jtag_axi.tcl")
    assert "source {C:/hw/tools/jtag_axi.tcl}" in session.scripts[0]
    jtag.JtagTransport()
    assert "KTPU_JAXI_TCL" in session.scripts[2]


def test_a_program_lands_the_same_words_as_the_memory_fake(session):
    """Same contract as every other backend: same bytes, same addresses."""
    prog = dev.Program()
    for slot in range(3):
        body = bytes((slot * 36 + i + 1) & 0xFF for i in range(36))
        prog.stage_flit(slot, int.from_bytes(body, "little"))
    prog.wr(0x58, 3).done(0xC0DE)

    ref = dev.MemoryTransport()
    prog.execute(ref)
    prog.execute(jtag.JtagTransport(tcl="x.tcl"))
    assert session.mem == ref.mem
