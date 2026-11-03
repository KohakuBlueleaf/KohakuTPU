"""A chained list (L0.NQ.057) on the hardware double: packages overlap, every WAIT
holds at the chain's running counts, and each way a chain fails is reported."""

import struct
import subprocess

import native
import pytest

from kohakutpu.application.runtime.os import abi as A

HEADER = struct.calcsize(A.PKG_HDR_FMT)
SOURCES = ["firmware/package/interp.c", "firmware/dispatch/engine.c"]
DISPATCH, BARRIER, END, MOVER, ENGINE = 1, 3, 0, 4, 11
POSTED = 1
SKIP = 2**64 - 1
BAD_STEP, BAD_MAGIC, TIMEOUT = 0x14, 0x10, 0x30
SLOW = 200
CREDIT = 2
A_UNIT, B_UNIT = (1, 2), (3, 4)
MB_DST, MB_GO, WAIT = 0, 5, 32
IL_RING = 24 + (0x10 >> 3)


def package(units, steps, payloads=()):
    """KAPK v2 (package-format.md s2) on `units`, each of credit `CREDIT`."""
    unit_at, step_at = HEADER, HEADER + 16 * len(units)
    pay_at = step_at + 16 * len(steps)
    h = [0] * (HEADER // 8)
    h[A.KA_PH_MAGIC] = HEADER << 48 | A.KA_PKG_VERSION << 32 | A.KA_PKG_MAGIC
    h[A.KA_PH_SIZE] = pay_at + 32 * len(payloads)
    h[A.KA_PH_COUNTS] = len(payloads)
    h[A.KA_PH_COUNTS2] = len(steps) << 16 | len(units) << 32
    h[A.KA_PH_OFF_UNIT], h[A.KA_PH_OFF_STEP] = unit_at, step_at
    h[A.KA_PH_OFF_PAY] = pay_at
    raw = struct.pack(A.PKG_HDR_FMT, *h)
    for x, y in units:
        raw += struct.pack("<2Q", 0x5643 << 32 | y << 8 | x, CREDIT)
    raw += b"".join(struct.pack("<2Q", *s) for s in steps)
    return raw + b"".join(struct.pack("<4Q", *p) for p in payloads)


def step(op, unit=0, count=0, arg=0, flags=0):
    return op | flags << 8 | unit << 16 | count << 32, arg


def words(n):
    return [(i + 1, 2, 3, 4) for i in range(n)]


def sends(n, units=(A_UNIT,), unit=0):
    """`n` words to `unit` of `units`, then the package's barrier."""
    steps = [step(DISPATCH, unit, n, 0), step(BARRIER), step(END)]
    return package(units, steps, words(n))


def wide():
    """More units than the engine counts: the firmware path's."""
    return sends(4, [(x % 8, 8 + x // 8) for x in range(17)])


def moves(n):
    """`n` posted moves."""
    steps = [step(MOVER, 0, n, 0, POSTED), step(BARRIER), step(END)]
    return package([A_UNIT], steps, [(0, 1 << 16, SKIP, 0)] * n)


def stream(n):
    """`n` words as the host lowers them: credit WAITs and a closing WAIT in the
    entries, counted from the package's start."""
    entries = [(MB_DST, A_UNIT[1] << 8 | A_UNIT[0])]
    for i in range(1, n + 1):
        if i > CREDIT:
            entries.append((WAIT, i - CREDIT))
        entries += [*((1 + a, i + a) for a in range(4)), (MB_GO, 1)]
    entries.append((WAIT, n))
    return engine(entries)


def engine(entries):
    """One ENGINE step of `entries` (code, value), three a payload."""
    groups = []
    for k in range(0, len(entries), 3):
        chunk = entries[k : k + 3]
        codes = sum(c << (8 * j) for j, (c, _) in enumerate(chunk))
        pad = 0xFF << (8 * len(chunk)) if len(chunk) < 3 else 0
        groups.append((codes | pad, *[v for _, v in chunk], *[0] * (3 - len(chunk))))
    return package([A_UNIT], [step(ENGINE, 0, len(entries), 0), step(END)], groups)


def run(exe, tmp_path, *lists, chain=1, engine=1, retire=SLOW, lost=0, extra=0):
    """Each list's ([(status, pending)], (status, clears, peak, gos, pending))."""
    args = []
    for i, packages in enumerate(lists):
        args += ["/"] if i else []
        for k, raw in enumerate(packages):
            path = tmp_path / f"l{i}p{k}.bin"
            path.write_bytes(raw)
            args.append(str(path))
    done = subprocess.run(
        [str(exe), *map(str, (engine, chain, retire, lost, extra)), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    out, packages = [], []
    for line in done.stdout.splitlines():
        kind, *fields = line.split()
        if kind == "P":
            packages.append(tuple(map(int, fields)))
        else:
            out.append((packages, tuple(map(int, fields))))
            packages = []
    return out


@pytest.fixture(scope="module")
def exe(tmp_path_factory):
    return native.on_double(tmp_path_factory.mktemp("chain"), "package_chain", SOURCES)


def test_a_chained_package_returns_with_its_words_still_running(exe, tmp_path):
    ((chained, _),) = run(exe, tmp_path, [sends(40), sends(40), sends(40)])
    ((serial, _),) = run(exe, tmp_path, [sends(40), sends(40), sends(40)], chain=0)
    assert [p[1] for p in chained][:2] != [0, 0]
    assert [p[1] for p in chained][2] == 0
    assert [p[1] for p in serial] == [0, 0, 0]


def test_the_engine_is_cleared_once_at_the_chain_start_and_once_at_its_end(
    exe, tmp_path
):
    ((_, chained),) = run(exe, tmp_path, [sends(4), sends(4), sends(4), sends(4)])
    ((_, serial),) = run(
        exe, tmp_path, [sends(4), sends(4), sends(4), sends(4)], chain=0
    )
    assert chained[0] == 0 and chained[1] == 2
    assert serial[0] == 0 and serial[1] == 8


def test_every_wait_holds_at_the_chain_running_count(exe, tmp_path):
    ((_, last),) = run(exe, tmp_path, [sends(40), sends(40), sends(40)])
    assert last[0] == 0 and last[2] == CREDIT and last[3] == 120 and last[4] == 0


def test_a_lowered_stream_is_rebased_on_the_running_count(exe, tmp_path):
    ((_, last),) = run(exe, tmp_path, [stream(30), stream(30), sends(10)])
    assert last[0] == 0 and last[2] == CREDIT and last[3] == 70


def test_a_unit_keeps_its_counter_at_another_table_index(exe, tmp_path):
    second = sends(30, (B_UNIT, A_UNIT), 1)
    ((_, last),) = run(exe, tmp_path, [sends(30), second, sends(30)])
    assert last[0] == 0 and last[2] == CREDIT and last[3] == 90


def test_moves_carry_across_the_chain(exe, tmp_path):
    ((_, last),) = run(exe, tmp_path, [moves(3), moves(3), sends(2)])
    assert last[0] == 0


def test_counts_wrap_the_twelve_bit_counters(exe, tmp_path):
    ((_, last),) = run(exe, tmp_path, [sends(1500)] * 3, retire=8)
    assert last[0] == 0 and last[3] == 4500 and last[2] <= CREDIT


def test_a_package_the_engine_cannot_hold_ends_the_chain_first(exe, tmp_path):
    ((packages, last),) = run(exe, tmp_path, [sends(30), wide(), sends(30)])
    assert last[0] == 0 and last[2] == CREDIT
    assert packages[1] == (0, 0)


def test_a_lost_retirement_in_a_held_package_is_reported(exe, tmp_path):
    ((_, last),) = run(exe, tmp_path, [sends(4), sends(4)], lost=1)
    assert last[0] == TIMEOUT


def test_a_lost_retirement_before_a_wide_package_is_reported(exe, tmp_path):
    ((_, last),) = run(exe, tmp_path, [sends(4), wide()], lost=1)
    assert last[0] == TIMEOUT


def test_a_count_past_what_the_chain_waited_for_is_reported(exe, tmp_path):
    ((_, last),) = run(exe, tmp_path, [sends(4), sends(30)], extra=1)
    assert last[0] == BAD_STEP


def test_a_failed_header_mid_chain_leaves_the_next_list_working(exe, tmp_path):
    bad = b"\0" * 256
    first, second = run(
        exe, tmp_path, [sends(20), bad, sends(20)], [sends(9), sends(9)]
    )
    assert first[1][0] == BAD_MAGIC and len(first[0]) == 2
    assert second[1][0] == 0 and second[1][2] == CREDIT and second[1][3] == 18


def test_engine_rings_take_the_interlink_room_ring_steps_take(exe, tmp_path):
    """F-003: the engine issues a RING with no room check, so a burst of raw
    RING entries is held by the firmware's ring count and none is dropped."""
    ((_, last),) = run(exe, tmp_path, [engine([(IL_RING, 1 << 8)] * 9), sends(2)])
    assert last[0] == 0 and last[5] == 9 and last[6] == 0, last


RING, CTRL = 5, 21
IL_RING_AT = 0xC0 + 0x10


def rings_every_way():
    """A RING step, five RINGs as CTRL writes, three ENGINE RING entries, a RING
    step: ten rings against a four-deep queue the sender drains slowly."""
    writes = [(IL_RING_AT, 1 << 8)] * 5 + [(SKIP, 0)]
    ctrl = [(*writes[k], *writes[k + 1]) for k in range(0, 6, 2)]
    entries = [(IL_RING, 1 << 8)] * 3
    codes = sum(c << (8 * j) for j, (c, _) in enumerate(entries))
    group = (codes, *[v for _, v in entries])
    steps = [
        step(RING, 0, 1, 1),
        step(CTRL, 0, len(ctrl), 0),
        step(ENGINE, 0, len(entries), len(ctrl)),
        step(RING, 0, 1, 1),
        step(BARRIER),
        step(END),
    ]
    return package([A_UNIT], steps, [*ctrl, group])


def test_ctrl_rings_take_the_interlink_room_ring_steps_take(exe, tmp_path):
    """F-003: a RING written as a CTRL pair counts against the same room as RING
    steps and ENGINE entries, so none of the ten is dropped."""
    ((_, last),) = run(exe, tmp_path, [rings_every_way(), sends(2)])
    assert last[0] == 0 and last[5] == 10 and last[6] == 0, last


MUTANTS = {
    "no_base": ("((want + de.base[c]) & KA_DE_CTR_MASK)", "(want & KA_DE_CTR_MASK)"),
    "no_slot": ("de.slot[u] = (uint8_t)c;", "de.slot[u] = (uint8_t)u;"),
    "no_rebase": ("    return de_target(src, want);\n}", "    return v;\n}"),
    "no_top": ("*top += ahead;", ""),
    "no_move_carry": ("de.base[KA_DE_MOVER] += ", "de.base[KA_DE_MOVER] += 0 * "),
    "no_settle": ("        de_total();\n        de_settled();\n", ""),
    "unchain_silent": (
        "    if (!de_drain()) {\n        de_settled();\n    }\n    de_stop();",
        "    de_stop();",
    ),
    "ring_unroomed": ("c == KA_DE_IL(KA_IL_RING) && ring_room()", "0"),
    "ctrl_ring_unroomed": ("else if (at == KA_R_IL + KA_IL_RING)", "else if (0)"),
}
CHECKS = {
    "no_base": test_every_wait_holds_at_the_chain_running_count,
    "no_slot": test_a_unit_keeps_its_counter_at_another_table_index,
    "no_rebase": test_a_lowered_stream_is_rebased_on_the_running_count,
    "no_top": test_a_lowered_stream_is_rebased_on_the_running_count,
    "no_move_carry": test_moves_carry_across_the_chain,
    "no_settle": test_a_count_past_what_the_chain_waited_for_is_reported,
    "unchain_silent": test_a_lost_retirement_before_a_wide_package_is_reported,
    "ring_unroomed": test_engine_rings_take_the_interlink_room_ring_steps_take,
    "ctrl_ring_unroomed": test_ctrl_rings_take_the_interlink_room_ring_steps_take,
}


@pytest.mark.parametrize("name", CHECKS)
def test_each_mutant_is_told(tmp_path, name):
    broken = native.on_double(tmp_path, "package_chain", SOURCES, MUTANTS[name])
    with pytest.raises(AssertionError):
        CHECKS[name](broken, tmp_path)
