"""Packages on the Xache home nearest their node: `arena.place` against the
address map the DRAM model is read through, and the one rule the replay
admission simulates."""

import json
import random
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kohakuaccel.driver.transport.simdram import DramMap
from kohakutpu.application.runtime.arena import Arena, place
from kohakutpu.application.runtime.memory import CardMemory, package_homes
from kohakutpu.application.runtime.replay_budget import select
from kohakutpu.compiler import _rs
from kohakutpu.stack import rs

ROOT = Path(__file__).resolve().parents[4]
BOARD = {"dram_interleave_kb": 16, "dram_flat_gb": 16, "dram_homes": 4}
MAP = DramMap.of_board(BOARD)
STRIPE, HOMES = 16 << 10, 4
SIZES = (256, STRIPE - 256, STRIPE, STRIPE + 256, 3 * STRIPE, 9 * STRIPE)
STARTS = (0, 256, STRIPE - 256, STRIPE, 3 * STRIPE + 512, 5 * STRIPE - 256)


def on_home(at: int, size: int) -> tuple:
    """`(home, bytes)` of the first run of `size` bytes at `at`."""
    _, home, _, n = next(MAP.runs(at, size))
    return home, n


def homes_kept(at_lo, size, home):
    spans = [(at_lo, 1 << 24)]
    at = place(spans, size, (STRIPE, HOMES, home))
    assert on_home(at, size) == (home, min(size, STRIPE))
    assert at >= at_lo
    assert sum(n for _, n in spans) == (1 << 24) - size


@pytest.mark.parametrize("home", range(HOMES))
@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("lo", STARTS)
def test_a_package_stays_on_its_home(lo, size, home):
    homes_kept(lo, size, home)


def test_mutant_a_package_only_starting_on_its_home_is_caught():
    assert "app.arena/home_start_only" in rs.mutants()
    with rs.mutant("app.arena/home_start_only"), pytest.raises(AssertionError):
        homes_kept(STRIPE - 256, STRIPE, 0)


def test_no_span_on_the_home_is_first_fit():
    """Space elsewhere takes the package: placement refuses nothing."""
    spans = [(STRIPE, 4096)]
    assert place(spans, 256, (STRIPE, HOMES, 0)) == STRIPE
    assert spans == [(STRIPE + 256, 4096 - 256)]
    assert place([(0, 256)], 512, (STRIPE, HOMES, 0)) is None


def test_near_none_is_first_fit():
    spans = [(0, 256), (4096, 4096)]
    assert place(spans, 512) == 4096
    assert spans == [(0, 256), (4608, 3584)]


def test_the_span_around_a_placed_package_stays_free():
    spans = [(256, 3 * STRIPE)]
    at = place(spans, 256, (STRIPE, HOMES, 2))
    assert at == 2 * STRIPE
    assert spans == [(256, at - 256), (at + 256, STRIPE)]


@pytest.mark.parametrize("native", [_rs.Arena, Arena])
def test_an_arena_places_as_the_rule_does(native):
    arena = native(0, 1 << 22)
    near = (STRIPE, HOMES, 3)
    spans = list(arena.free_list)
    want = place(spans, 4096, near)
    assert arena.alloc(4096, "package", near) == want
    assert arena.free_list == spans
    assert arena.alloc(256) == 0


def test_the_native_arena_is_the_arena():
    same_places()
    with rs.mutant("app.arena/home_start_only"), pytest.raises(AssertionError):
        same_places()


def same_places():
    rng = random.Random(7)
    arenas = _rs.Arena(0x1000, 1 << 26), Arena(0x1000, 1 << 26)
    live = []
    for _ in range(2000):
        if live and rng.random() < 0.45:
            at = live.pop(rng.randrange(len(live)))
            for arena in arenas:
                arena.release(at)
        else:
            size = rng.choice((1, 256, 300, 4096, STRIPE - 64, STRIPE, STRIPE * 2 + 1))
            home = rng.randrange(HOMES + 1)
            near = None if home == HOMES else (STRIPE, HOMES, home)
            at = [arena.alloc(size, "package", near) for arena in arenas]
            assert at[0] == at[1]
            live.append(at[0])
        assert arenas[0].free_list == arenas[1].free_list


class Queue:
    """What `select` asks of a queue that holds nothing."""

    sq_n = cq_n = 4

    def prepared_addresses(self):
        return ()

    def has_prepared(self, raw):
        return False

    def is_cached(self, raw):
        return False


def test_admission_simulates_the_placement():
    """A small package placed on home 1 cuts the arena's one span, so the large
    one no longer fits though the free bytes do: admission does not pin it."""
    memory = CardMemory((0, 3 * STRIPE), {})
    memory.stripes, memory.package_homes = (STRIPE, HOMES), {0: 1}
    small, large = bytes(200), bytes(2 * STRIPE)
    log = [("run", {0: (raw, ())}) for raw in (small, small, large)]
    card = SimpleNamespace(memory=memory, queues={0: Queue()})
    _, chosen, _, _ = select(card, log, 3 * STRIPE, 1)
    assert (0, large) not in chosen
    memory.package_homes = {}
    _, chosen, _, _ = select(card, log, 3 * STRIPE, 1)
    assert set(chosen) == {(0, small), (0, large)}


def test_each_node_takes_its_own_home():
    memory = CardMemory((0, 1 << 22), {})
    memory.stripes = STRIPE, HOMES
    memory.package_homes = package_homes("near", board("vlt_v9t6_2n"))
    assert memory.package_homes == {0: 0, 1: 1}
    for node in memory.package_homes:
        at = memory.alloc(1024, "package", memory.near(node))
        assert on_home(at, 1024)[0] == node
    assert memory.near(2) is None


@pytest.mark.parametrize(
    "value, homes", [("any", {}), ("3", {0: 3}), ("0,2", {0: 0, 1: 2})]
)
def test_the_knob(value, homes):
    assert package_homes(value, board("multimesh_v9t6")) == homes


def board(name):
    return json.loads((ROOT / "boards" / f"{name}.json").read_text())


def test_the_boards_hold_the_xaches_homes():
    got = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, gen_board; print(json.dumps(gen_board.xache_homes()))",
        ],
        cwd=ROOT / "scripts" / "py",
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    facts = json.loads(got.stdout)
    assert facts == {"dram_homes": 4, "dram_home_of_die": [0, 1, 2, 3]}
    for path in (ROOT / "boards").glob("*.json"):
        spec = json.loads(path.read_text())
        if "dram_interleave_kb" in spec:
            assert {k: spec[k] for k in facts} == facts, path.name
