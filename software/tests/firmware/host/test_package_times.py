"""A run's first and last send times (L0.NQ.056) against the sends a hardware double
saw, through the dispatch engine and on the firmware path."""

import struct
import subprocess

import native
import pytest

from kohakutpu.application.runtime.os import abi as A

HEADER = struct.calcsize(A.PKG_HDR_FMT)
SOURCES = ["firmware/package/interp.c", "firmware/dispatch/engine.c"]
WORDS, SETTLE = 40, 3000
DISPATCH, AWAIT, BARRIER, END, SETTLE_OP, ENGINE = 1, 2, 3, 0, 8, 11
MB_DST, MB_GO = 0, 5
#: Entries the engine may still hold when the last step's stamp is taken.
QUEUED = 256
#: Register accesses between a firmware stamp and the GO it stands for.
NEAR = 8
MUTANTS = {
    "stale_stamps": ("e->t_first = e->t_last = 0;", ""),
    "first_after_the_step": (
        "eng.t_first = eng.t_first ? eng.t_first : t;",
        "eng.t_first = eng.t_first ? eng.t_first : ka_cycles() + (t & 0);",
    ),
    "every_step_stamps": ("if (de_sends(w0)) {", "if (de_sends(w0) || 1) {"),
}


def package(steps, payloads=()):
    """KAPK v2 (package-format.md s2): one VC at (1, 2), `steps`, `payloads`."""
    unit_at, step_at = HEADER, HEADER + 16
    pay_at = step_at + 16 * len(steps)
    h = [0] * (HEADER // 8)
    h[A.KA_PH_MAGIC] = HEADER << 48 | A.KA_PKG_VERSION << 32 | A.KA_PKG_MAGIC
    h[A.KA_PH_SIZE] = pay_at + 32 * len(payloads)
    h[A.KA_PH_COUNTS] = len(payloads)
    h[A.KA_PH_COUNTS2] = len(steps) << 16 | 1 << 32
    h[A.KA_PH_OFF_UNIT], h[A.KA_PH_OFF_STEP] = unit_at, step_at
    h[A.KA_PH_OFF_PAY] = pay_at
    raw = struct.pack(A.PKG_HDR_FMT, *h)
    raw += struct.pack("<2Q", 0x5643 << 32 | 2 << 8 | 1, 2)
    raw += b"".join(struct.pack("<2Q", *s) for s in steps)
    return raw + b"".join(struct.pack("<4Q", *p) for p in payloads)


def step(op, unit=0, count=0, arg=0):
    return op | unit << 16 | count << 32, arg


def words():
    return [(i + 1, 2, 3, 4) for i in range(WORDS)]


def dispatched():
    """Words between two settles, so a stamp taken at the wrong step is far off."""
    steps = [
        step(SETTLE_OP, count=SETTLE),
        step(DISPATCH, 0, WORDS, 0),
        step(SETTLE_OP, count=SETTLE),
        step(BARRIER),
        step(END),
    ]
    return package(steps, words())


def lowered():
    """The same words as the host lowers them: one ENGINE step of mailbox entries."""
    entries = []
    for i in range(WORDS):
        entries += [
            (MB_DST, 2 << 8 | 1),
            *((1 + a, i + a) for a in range(4)),
            (MB_GO, 1),
        ]
    entries.append((32, WORDS))
    groups = []
    for k in range(0, len(entries), 3):
        chunk = entries[k : k + 3]
        codes = sum(c << (8 * j) for j, (c, _) in enumerate(chunk))
        values = [v for _, v in chunk] + [0] * (3 - len(chunk))
        groups.append(
            (codes | 0xFF << (8 * len(chunk)) if len(chunk) < 3 else codes, *values)
        )
    steps = [
        step(ENGINE, 0, len(entries), 0),
        step(SETTLE_OP, count=SETTLE),
        step(BARRIER),
        step(END),
    ]
    return package(steps, groups)


def silent():
    return package([step(SETTLE_OP, count=100), step(BARRIER), step(END)])


def times(exe, tmp_path, engine, *packages):
    """(status, sent, first, last, double's first, double's last, GOs) a package."""
    files = []
    for i, raw in enumerate(packages):
        files.append(tmp_path / f"p{i}.bin")
        files[-1].write_bytes(raw)
    done = subprocess.run(
        [str(exe), str(engine), *map(str, files)],
        capture_output=True,
        text=True,
        check=True,
    )
    return [tuple(map(int, ln.split())) for ln in done.stdout.splitlines()]


@pytest.fixture(scope="module")
def exe(tmp_path_factory):
    return native.on_double(tmp_path_factory.mktemp("times"), "package_times", SOURCES)


def told(result, near):
    status, _, first, last, go_first, go_last, gos = result
    assert status == 0 and gos == WORDS
    assert first - NEAR <= go_first <= first + near, result
    assert last - NEAR <= go_last <= last + near, result


def test_firmware_path_stamps_each_go(exe, tmp_path):
    (r,) = times(exe, tmp_path, 0, dispatched())
    assert r[1] == WORDS
    told(r, NEAR)


def test_engine_stamps_the_step_that_queued_the_words(exe, tmp_path):
    (r,) = times(exe, tmp_path, 1, dispatched())
    assert r[1] == WORDS
    assert r[2] >= SETTLE
    told(r, QUEUED)


def test_a_lowered_stream_is_stamped_though_no_word_is_counted(exe, tmp_path):
    (r,) = times(exe, tmp_path, 1, lowered())
    assert r[1] == 0
    told(r, QUEUED)


@pytest.mark.parametrize("engine", [0, 1])
def test_a_run_with_no_send_reads_zero_after_one_with_sends(exe, tmp_path, engine):
    _, r = times(exe, tmp_path, engine, dispatched(), silent())
    assert r[2:4] == (0, 0) and r[6] == 0


@pytest.mark.parametrize("name", MUTANTS)
def test_each_mutant_is_told(tmp_path, name):
    exe = native.on_double(tmp_path, "package_times", SOURCES, MUTANTS[name])
    with pytest.raises(AssertionError):
        if name == "stale_stamps":
            assert times(exe, tmp_path, 1, lowered(), silent())[1][2:4] == (0, 0)
        else:
            told(times(exe, tmp_path, 1, lowered())[0], QUEUED)
