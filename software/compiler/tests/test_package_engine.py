"""Packages lowered for the dispatch engine (`kohakuaccel.compiler.package.engine`),
run against the reference interpreter."""

import struct

import pytest
from kohakuaccel.compiler.package import engine
from kohakuaccel.compiler.package.format import (
    F_POSTED,
    MOVER_SKIP,
    Op,
    Package,
    PackageError,
    Reloc,
    Step,
    Unit,
    type_code,
)
from kohakuaccel.simulation.package import Interpreter

MG, VC = type_code("MG"), type_code("VC")
PORT = 1 << 16 | 1 << 8 | 3  # memory port (3, 1)
GO_MOVE = 1 << 16 | 5


def _pkg(steps, payloads, units=None) -> Package:
    return Package(
        units=units or [Unit(MG, 1, 0, 0, 512, PORT), Unit(VC, 1, 2, 0, 512, 0)],
        steps=steps,
        payloads=payloads,
    )


def _small():
    words = [0x11 | 1 << 200, 0x22, 0x33]
    vword = 0xA | 0xB << 64 | 0xC << 128 | 0xD << 192
    move = 0x08 | 0xA0 << 64 | 0 << 128 | GO_MOVE << 192
    steps = [
        Step(Op.DISPATCH, 0, 3, 0),
        Step(Op.AWAIT, 0, 3),
        Step(Op.DISPATCH, 1, 1, 3),
        Step(Op.MOVER, 0, 1, 4, F_POSTED),
        Step(Op.MWAIT, 0, 1),
        Step(Op.RING, 1, 7),
        Step(Op.SIGNAL, 0, 9, 0),
        Step(Op.BARRIER),
    ]
    return _pkg(steps, [*words, vword, move])


def test_a_small_package_lowers_to_the_engine_writes_and_waits():
    low = engine.lower(_small())
    assert [s.op for s in low.steps] == [Op.ENGINE, Op.SIGNAL, Op.ENGINE]
    first = engine.entries(low, low.steps[0])
    assert first == [
        (0, 1 << 24 | 0x0103),  # DST: typed MEM_RD_REQ to port (3, 1)
        (1, 0),
        (2, 0),
        (3, 1 << 40 | 1 << 30),  # ARG2: peer (1, 0), INST
        (4 | engine.REL, 0 << 24 | 0x60 << 8 | 3),  # ARG3: payload 0, 3 words
        (5, 1),
        (32, 0 << 56 | 3),  # AWAIT MG 3
        (0, 2 << 8 | 1),  # the vector core's word through the mailbox
        (1, 0xA),
        (2, 0xB),
        (3, 0xC),
        (4, 0xD),
        (5, 1),
        (9, 0xA0),  # mover register 0x08
        (8, GO_MOVE),  # mover CTRL, GO
        (32, 16 << 56 | 1),  # MWAIT 1
        (26, 7 << 8 | 1),  # RING mesh 1, tag 7
    ]
    tail = engine.entries(low, low.steps[2])
    waits = [(32, 0 << 56 | 3), (32, 1 << 56 | 1), (32, 16 << 56 | 1)]
    assert tail == waits + waits  # the BARRIER, then the package's end


def test_entries_pack_three_to_a_payload_codes_first():
    low = engine.lower(_small())
    s = low.steps[0]
    blob = low.to_bytes()
    opay = struct.unpack_from("<Q", blob, 9 * 8)[0]
    at = opay + s.arg * 32
    got = blob[at : at + 32]
    want = struct.pack("<BBB5xQQQ", 0, 1, 2, 1 << 24 | 0x0103, 0, 0)
    assert got == want
    # the last payload of the stream: 17 entries, so two codes and a NONE
    last = blob[at + 5 * 32 : at + 6 * 32]
    assert last == struct.pack("<BBB5xQQQ", 32, 26, 0xFF, 16 << 56 | 1, 7 << 8 | 1, 0)


def test_fetch_runs_are_cut_at_the_credit_with_waits_between():
    units = [Unit(MG, 1, 0, 0, 4, PORT)]
    low = engine.lower(_pkg([Step(Op.DISPATCH, 0, 10, 0)], list(range(10)), units))
    got = engine.entries(low, low.steps[0])
    arg3 = [(v >> 24, v & 0xFF) for c, v in got if c == 4 | engine.REL]
    assert arg3 == [(0, 4), (4 * 32, 4), (8 * 32, 2)]
    waits = [v & engine.CTR_MASK for c, v in got if c == engine.WAIT]
    assert waits == [4, 6, 10]  # credit before runs 2 and 3, then the end


def test_a_repeat_is_spelled_out_and_fetched():
    template = [0x100, 0x200]
    inc = 0 | 8 << 32 | 8 << 40 | 1 << 64  # word 0, bits [8, 16) += 1
    steps = [Step(Op.REPEAT, 0, 2, 0 | 3 << 32 | 1 << 48)]
    low = engine.lower(_pkg(steps, [*template, inc], [Unit(MG, 1, 0, 0, 512, PORT)]))
    assert low.payloads[3:9] == [0x100, 0x200, 0x200, 0x200, 0x300, 0x200]
    arg3 = [v for c, v in engine.entries(low, low.steps[0]) if c == 4 | engine.REL]
    assert arg3 == [3 * 32 << 24 | 0x60 << 8 | 6]


class _Units:
    """Every word sent completes at once (INST_COMPLETE)."""

    def __init__(self) -> None:
        self.sent: list = []
        self.owed: list = []

    def send(self, x, y, payload) -> None:
        self.sent.append((x, y, payload))
        self.owed.append((x, y, 0, 0))

    def drain(self):
        out, self.owed = self.owed, []
        return out


def _run(pkg: Package):
    units, moves = _Units(), []
    res = Interpreter(units, mover=moves.append).run(pkg.to_bytes())
    return res, units.sent, [w for batch in moves for w in batch]


def test_the_reference_runs_a_lowered_package_as_the_original():
    pkg = _small()
    res0, sent0, moves0 = _run(pkg)
    res1, sent1, moves1 = _run(engine.lower(pkg))
    assert res0.status == res1.status == 0
    assert sent1 == sent0 and len(sent0) == 4
    assert moves1 == [(r, v) for r, v in moves0 if r != MOVER_SKIP]


def test_a_fetch_reads_an_absolute_address_with_no_rel():
    units = [Unit(MG, 1, 0, 0, 4, PORT)]
    at = 0x80_0000_1000
    low = engine.lower(_pkg([Step(Op.FETCH, 0, 6, at)], [], units))
    got = engine.entries(low, low.steps[0])
    assert not [c for c, _ in got if c & engine.REL]
    arg3 = [(v >> 24, v & 0xFF) for c, v in got if c == 4]
    assert arg3 == [(at, 4), (at + 4 * 32, 2)]


def test_the_reference_fetches_from_memory_as_the_engine_does():
    words = [0x1234 << k for k in range(5)]
    mem = b"".join(w.to_bytes(32, "little") for w in words)
    base = 0x4000

    def memory(addr, n):
        return mem[addr - base : addr - base + n]

    pkg = _pkg([Step(Op.FETCH, 0, 5, base), Step(Op.BARRIER)], [])
    for p in (pkg, engine.lower(pkg)):
        units = _Units()
        res = Interpreter(units, memory=memory).run(p.to_bytes())
        assert res.status == 0
        assert units.sent == [(1, 0, w) for w in words]


def test_a_fetch_needs_a_port():
    with pytest.raises(PackageError, match="port"):
        _pkg([Step(Op.FETCH, 1, 2, 0x1000)], []).to_bytes()


def test_lowering_refuses_what_the_engine_cannot_carry():
    relocated = _small()
    relocated.relocs = [Reloc(0, 0, 32, 0, 0)]
    with pytest.raises(PackageError, match="bound"):
        engine.lower(relocated)
    many = [Unit(MG, i % 16, i // 16, 0, 512, PORT) for i in range(17)]
    with pytest.raises(PackageError, match="units"):
        engine.lower(_pkg([Step(Op.BARRIER)], [], many))
    with pytest.raises(PackageError, match="lowered"):
        engine.lower(engine.lower(_small()))
