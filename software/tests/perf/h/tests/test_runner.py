"""The h runner on a card double: the A1 generator's module compiles and passes the
static checker; each run binds every parameter inside its redzones, paints, runs
and checks its outputs word for word; a ledger fn's die counters and slot words
decide its verdict; a triple fn's phases run serial -> overlap -> serial; the
meta names the firmware image and brackets every sysnode's cycle offset."""

import hashlib
import struct
from types import SimpleNamespace as NS

from h import gen, meta, runner
from kohakutpu.application.runtime import packages as B
from kohakutpu.application.runtime.card.common import MACHINES
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.driver.clock.card import load_board
from kohakutpu.driver.host import board_map
from kohakutpu.language.text import module as M
from kohakutpu.stack import kernel

BOARD = "multimesh_v9t6"
#: AUX_STATW words: rings from source 0, rings sent.
STAT_FROM0, STAT_SENT = 0x80 + 8 * 2, 0x80 + 8 * 14


def compiled(row, sysnodes, name):
    mod = gen.load(row, sysnodes, BOARD)
    machine = machine_of(BOARD, nodes=max(sysnodes) + 1)
    text = M.read(mod.source, f"<{row}>")
    return mod, kernel.compile(text, name, machine, level="l1")


def test_a1_pings_every_ordered_pair_both_ways_and_the_checker_passes_each():
    sysnodes = (0, 1, 2, 3)
    mod = gen.load("a1", sysnodes, BOARD)
    text = M.read(mod.source, "<a1>")
    machine = machine_of(BOARD, nodes=4)
    assert len(mod.specs) == 12 * 2 * 2
    assert sorted(mod.specs) == sorted(text.names())
    for name in mod.specs:
        progs = kernel.compile(text, name, machine, level="l1").programs(
            {"count": 0x10_0000}
        )
        pair = tuple(int(c) for c in name.split("_")[1])
        assert set(progs) == set(pair)
        assert B.findings(progs, "refuse") == []


class Card:
    """What the runner reaches of a card: flat memory placed upward from a mark,
    every die's control window, and a run that does what `act` says."""

    def __init__(self, act):
        self.board = load_board(BOARD)
        self.machine_json = (MACHINES / f"{BOARD}.json").read_text("utf-8")
        self.nodes = (0, 1)
        self.t = NS(words={}, writes=[])
        self.t.read64 = lambda at: self.t.words.get(at, 0)
        self.t.write64 = lambda at, v: self.t.writes.append((at, v))
        self.bytes, self.top, self.spans, self.seen = {}, 0x10_0000, [], []
        self.live: dict = {}
        arena = NS(live=self.live, owners=self.live)
        self.memory = NS(arenas=lambda: (arena,))
        self.act = act

    def mark(self):
        return self.top

    def release(self, mark):
        self.top = mark
        for at in [a for a in self.live if a >= mark]:
            del self.live[at]

    def place(self, nbytes, space, node):
        at, self.top = self.top, self.top + nbytes
        self.spans.append(at)
        self.live[at] = nbytes
        return at

    def bound(self, k: int) -> int:
        """Where the runner bound the k-th parameter."""
        return self.spans[k] + runner.PAD["dram"]

    def write(self, at, raw):
        for k, b in enumerate(raw):
            self.bytes[at + k] = b

    def get(self, at, nbytes):
        return bytes(self.bytes.get(at + k, 0) for k in range(nbytes))

    def run(self, prog, faults):
        assert faults
        self.seen.append(self.get(self.bound(0), 16))
        return self.act(self, prog)

    def bump(self, die: int, word: int, by: int) -> None:
        at = board_map(self.board)["ctrl"][die] + word
        self.t.words[at] = self.t.words.get(at, 0) + by


def runs(card, row, name, reps=1, sysnodes=(0, 1)):
    mod, c = compiled(row, sysnodes, name)
    return list(runner.runs(card, c, mod.specs[c.name], 7, reps, True, None, "s"))


def test_each_run_paints_runs_and_checks_its_outputs_word_for_word():
    def act(card, prog):
        assert sorted(prog) == [0, 1]
        card.write(card.bound(0), struct.pack("<2Q", 8, 8))
        card.bump(1, STAT_SENT, 8)
        one = dict(cycles=40, phases={"head": 3}, steps=[(0, 1, 9, "RING")])
        return dict(cycles=42, nodes={0: dict(one, pkg=object()), 1: one})

    card = Card(act)
    rows = runs(card, "a1", "a1_01_fenced_k8", reps=3)

    assert card.seen == [b"\xa5" * 16] * 3  # painted before every run
    assert card.top == 0x10_0000  # released
    want = hashlib.sha1(struct.pack("<2Q", 8, 8)).hexdigest()[:12]
    for rep, (row, units) in enumerate(rows):
        assert (row["rep"], row["epoch"], row["session"], units) == (
            rep,
            rep + 1,
            "s",
            [],
        )
        assert row["outputs"]["count"]["digest"] == want
        assert row["outputs"]["count"]["mismatched"] == 0
        assert row["verdict"] == dict(
            structural="PASS", precision="NONE", reasons=[], timing="VALID"
        )
        assert row["binds"]["count"] == card.bound(0)
        assert row["redzones"]["count"] == [card.bound(0) - 256, card.bound(0) + 64]
        at = card.bound(0)
        assert row["intervals"]["params"]["count"] == [at, at + 16, at, at + 64, "dram"]
        assert [at - runner.PAD["dram"], at + 16 + runner.PAD["dram"]] == row[
            "intervals"
        ]["held"][0][:2]
        assert row["interlink"][1]["bells_sent"] == 8
        assert row["interlink"][0]["bells_sent"] == 0


def test_an_output_left_poison_or_a_redzone_hit_fails_the_run():
    def act(card, prog):
        card.write(card.bound(0) + 64, b"\0")  # the word past `count`'s line
        return dict(cycles=5, nodes={0: dict(fault="timeout"), 1: dict(cycles=5)})

    (row, _), *_ = runs(Card(act), "a1", "a1_01_plain_k1")

    got = row["outputs"]["count"]
    assert got["digest"] == hashlib.sha1(b"\xa5" * 16).hexdigest()[:12]
    assert got["poison_left"] == 2
    # The quiet reread of the mismatched output: the same words, no change.
    assert got["reread"]["mismatched"] == got["mismatched"]
    assert got["reread"]["changed"] == 0 and got["reread"]["changes"] == []
    assert row["verdict"]["structural"] == "FAIL"
    assert row["verdict"]["reasons"] == [
        "output count",
        "redzone count: [32]",
        "sysnode 0: timeout",
    ]


def test_a_ledger_fn_passes_only_on_its_exact_ring_counts_and_empty_slots():
    def act(card, prog, sent=1):
        card.write(card.bound(1), struct.pack("<Q", 1 << 50))
        card.bump(1, STAT_FROM0, 1)
        card.bump(0, STAT_SENT, sent)
        return dict(cycles=5, nodes={0: dict(cycles=5), 1: dict(cycles=5)})

    (row, _), *_ = runs(Card(act), "q0", "q0_ring_01_a3")
    assert row["ledger"] == [] and row["verdict"]["structural"] == "PASS"

    (row, _), *_ = runs(Card(lambda c, p: act(c, p, 2)), "q0", "q0_ring_01_a3")
    assert row["ledger"] == ["die 0 bells_sent: 2, ledger 1"]
    assert row["verdict"]["structural"] == "FAIL"


def test_a_triple_runs_each_group_serial_then_overlapped_then_serial():
    mod = gen.load("c1", (0, 1, 2), BOARD)
    assert sorted(mod.specs) == ["c1_chain_012", "c1_mgvc_012", "c1_vc_012"]
    spec = mod.specs["c1_vc_012"]
    got = runner.phases(spec, {0: None, 1: None, 2: None})
    first = [(p, r) for p, r in got if p.endswith(":01")]
    assert first == [
        ("before:01", (0,)),
        ("before:01", (1,)),
        ("overlap:01", (0, 1)),
        ("after:01", (0,)),
        ("after:01", (1,)),
    ]
    assert [p for p, _ in got if p.startswith("overlap")] == [
        "overlap:01",
        "overlap:02",
        "overlap:12",
        "overlap:012",
    ]
    assert spec.owners == {"y0": 0, "d0": 0, "y1": 1, "d1": 1, "y2": 2, "d2": 2}


def test_an_expected_timeout_issues_only_its_sysnodes_and_checks_as_expected():
    mod = gen.load("c5", (0, 1, 2, 3), BOARD)
    spec = mod.specs["c5_missing_producer"]
    assert runner.phases(spec, {n: None for n in range(4)}) == [
        ("issued:123", (1, 2, 3))
    ]

    def act(card, prog):
        assert sorted(prog) == [1, 2, 3]
        return dict(cycles=5, nodes={1: dict(fault="TIMEOUT wait slot 9")})

    (row, _), *_ = runs(Card(act), "c5", "c5_missing_producer", sysnodes=(0, 1, 2, 3))
    assert row["verdict"]["reasons"][-1] == "expected sysnode 1: TIMEOUT wait slot 9"
    assert row["outputs"]["z"]["mismatched"] == 0  # wanted POISON, left POISON
    assert row["outputs"]["y0"]["mismatched"] == 0  # its sysnode was not issued
    assert row["outputs"]["y1"]["poison_left"] > 0  # the double copied nothing
    assert row["verdict"]["structural"] == "FAIL"


def test_a_tracked_image_is_named_by_its_blob_an_untracked_one_by_size_and_mtime():
    """In a git tree a tracked file is its HEAD blob (an unchanged file); a copied
    tree without git (the Linux host) names it by size and mtime."""
    rel = "software/tests/perf/memgrid.py"
    tracked = meta.ROOT / rel
    if (meta.ROOT / ".git").exists():
        want = dict(path=str(tracked), blob=meta.git("rev-parse", f"HEAD:{rel}"))
    else:
        st = tracked.stat()
        want = dict(path=str(tracked), bytes=st.st_size, mtime=st.st_mtime)
    assert meta.image(tracked) == want

    loose = meta.ROOT / "build" / "h-tests" / "image.elf"
    loose.parent.mkdir(parents=True, exist_ok=True)
    loose.write_bytes(b"\0" * 24)
    try:
        got = meta.image(loose)
        assert got["bytes"] == 24 and got["mtime"] == loose.stat().st_mtime
    finally:
        loose.unlink()


def test_each_offset_is_bracketed_by_two_reads_of_the_first_sysnode():
    first = iter(range(1000, 2000, 10))
    card = NS(
        nodes=(0, 2),
        boots={
            0: (NS(win=NS(cycles=lambda: next(first))), None),
            2: (NS(win=NS(cycles=lambda: 6000)), None),
        },
    )

    got = meta.timebase(card)

    assert (got[0]["lo"], got[0]["hi"]) == (-10, 10)
    assert got[2]["first"] == (1030, 1040)
    assert (got[2]["lo"], got[2]["hi"]) == (4960, 4970)
    later = {
        n: dict(r, lo=r["lo"] + 3, hi=r["hi"] + 5, first=(5000, 5010))
        for n, r in got.items()
    }
    assert meta.drift(got, later)[2] == dict(lo=-7, hi=15, over=3960)
