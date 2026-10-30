"""Every spec vector against the current stack: a frozen vector passes and
fails under each mutant it names; a ruling vector fails until the stack
follows its ruling (strict xfail, so a pass is reported). A clause runs on
poisoned card memory, a mutant on a fresh stack (`harness.fresh`).

Tier 1 runs on a card session (``KTPU_SPEC_CARD=<board>``, one session for
every tier-1 vector); tier 2 on a Verilator build (``KTPU_SPEC_VERILATOR``).
"""

import contextlib
import os
import struct
import time
from types import SimpleNamespace

import pytest
import runners
from harness import fresh as F
from harness import vectors
from harness.fresh import fresh, poisoned
from harness.registry import MUTANTS, RUNNERS, SHAPED
from runners.card import Session

from kohakuaccel.driver.node.queue import NodeQueue, OverBudget
from kohakuaccel.driver.transport.base import TransportUnavailable
from kohakuaccel.driver.transport.memory import MemoryTransport
from kohakutpu.application.runtime import card as RC
from kohakutpu.application.runtime.memory import CardMemory
from kohakutpu.language import machine
from kohakutpu.language.text.syntax import TextError
from kohakutpu.stack import rs

assert runners.LAYERS
VECTORS = vectors.load()
TIER_ENV = {1: "KTPU_SPEC_CARD", 2: "KTPU_SPEC_VERILATOR"}


def ids(vs):
    return [v.id for v in vs]


@pytest.fixture(scope="session")
def session():
    """The tier-1 card session, opened once; None when not asked for."""
    board = os.environ.get("KTPU_SPEC_CARD")
    if not board:
        yield None
        return
    s = Session(board)
    yield s
    s.close()


def context(v, session):
    """A vector's context. On a tier-1 target the machine is the target's own
    (its machine file); a vector of a runner whose output is the die
    (`SHAPED`) is skipped on a target of another die."""
    tier = v["tier"]
    if tier and not os.environ.get(TIER_ENV[tier]):
        pytest.skip(f"tier {tier}: set {TIER_ENV[tier]}")
    if tier == 1 and session is not None:
        why = session.differs(v["machine"]) if v["runner"] in SHAPED else None
        if why:
            pytest.skip(f"tier 1: {why}")
        session.ready()
        return {"vector": v, "machine": session.machine, "session": session}
    m = machine.load(v["machine"]) if v.get("machine") else None
    return {"vector": v, "machine": m, "session": session}


#: Refusals of a program naming a unit the target's die lacks.
UNIT_CLAUSES = ("L1.UNIT.003",)


def run(v, ctx):
    """The vector's runner. On a tier-1 target of another die than the
    vector's machine, a program the compiler refuses for naming a unit the
    target lacks (`UNIT_CLAUSES`) is skipped with the reason, as a shaped
    vector is (`context`); on its own die that refusal is a failure."""
    try:
        return RUNNERS[v["runner"]](v["input"], ctx)
    except Exception as e:
        session = ctx.get("session")
        clause = next((c for c in UNIT_CLAUSES if c in str(e)), None)
        if clause and v["tier"] == 1 and session is not None and v.get("machine"):
            why = session.differs(v["machine"])
            if why:
                line = str(e).splitlines()[0][:200]
                pytest.skip(
                    f"tier 1: the program names a unit the target lacks ({line}); {why}"
                )
        raise


@pytest.mark.parametrize("die", ["another", "its own"])
def test_a_unit_the_target_lacks_skips_only_on_another_die(die, monkeypatch):
    def refuses(inp, ctx):
        raise TextError("L1.UNIT.003: vc[1]: this machine has 1 in mesh 0")

    monkeypatch.setitem(RUNNERS, "harness.refuses", refuses)
    why = "the vector's die is not the target's" if die == "another" else None
    session = SimpleNamespace(differs=lambda name: why)
    v = vectors.Vector(
        vectors.ROOT / "x.json",
        {
            "tier": 1,
            "runner": "harness.refuses",
            "input": {},
            "machine": "multimesh_v9t6",
        },
    )
    if why:
        with pytest.raises(pytest.skip.Exception, match="L1.UNIT.003"):
            run(v, {"session": session})
    else:
        with pytest.raises((ValueError, TextError), match="L1.UNIT.003"):
            run(v, {"session": session})


def test_missing_unit_clause_mutant_is_detected(monkeypatch):
    monkeypatch.setitem(run.__globals__, "UNIT_CLAUSES", ())
    with pytest.raises(TextError, match="vc"):
        test_a_unit_the_target_lacks_skips_only_on_another_die("another", monkeypatch)


@pytest.mark.parametrize("v", VECTORS, ids=ids(VECTORS))
def test_vector_is_well_formed(v):
    assert vectors.check(v) == []
    assert v["runner"] in RUNNERS, f"no runner {v['runner']}"
    for name in v["mutants"]:
        assert name in MUTANTS, f"no mutant {name}"


@contextlib.contextmanager
def over_budget(session, record_property):
    """A test whose wait outran its wall-clock alarm (`OverBudget`: a model's
    waits count modelled time, the alarm wall seconds) is over budget, neither
    a pass, a fail nor a kill: the card is its evidence. Skipped; on the
    session's Verilated card with the model's sysnode cycles and rate, and the
    node restarted, leaving the package (its queue would wait for it)."""
    t = session.dev.card.t if session and session.dev.kind == "sim" else None
    c0, w0 = t.status()["sys"] if t else 0, time.monotonic()
    try:
        yield
    except OverBudget as e:
        wall = time.monotonic() - w0
        if t is None:
            record_property("over_budget", {"seconds": round(wall, 1)})
            pytest.skip(f"over budget: {e}")
        cycles = t.status()["sys"] - c0
        record_property("over_budget", {"cycles": cycles, "seconds": round(wall, 1)})
        session.dev.card.restart()
        pytest.skip(
            f"over budget on the Verilated card: {e}; the model ran {cycles} "
            f"sysnode cycles in {wall:.0f} s ({cycles / wall:.0f}/s)"
        )


def record_untouched(seen, record_property) -> None:
    """At the test's end, the poisoned allocations its run never wrote,
    as the ``untouched`` property (a finding, not a failure)."""

    def report():
        if seen.untouched:
            record_property("untouched", seen.untouched)

    seen.report = report


#: Vector id -> whether its clause passed in this process: a mutant of a
#: vector whose clause failed here (wrong board, broken model) kills nothing.
BASELINE: dict = {}
BASELINE_SKIPS: dict = {}


@pytest.mark.parametrize("v", VECTORS, ids=ids(VECTORS))
def test_clause(v, session, record_property):
    ctx = context(v, session)
    with poisoned(session) as seen, over_budget(session, record_property):
        record_untouched(seen, record_property)
        if v["status"] == "ruling":
            try:
                got = run(v, ctx)
            except Exception as e:  # noqa: BLE001 -- any failure is "not yet"
                pytest.xfail(f"ruling not yet followed: {e}")
            if got != ctx.get("expect", v["expect"]):
                pytest.xfail("ruling not yet followed")
            raise AssertionError("the stack follows this ruling now: mark it frozen")
        BASELINE_SKIPS.pop(v.id, None)
        BASELINE[v.id] = False
        try:
            assert run(v, ctx) == ctx.get("expect", v["expect"])
        except pytest.skip.Exception as e:
            BASELINE_SKIPS[v.id] = str(e)
            raise
        BASELINE[v.id] = True


def test_skipped_baseline_skips_its_mutant(monkeypatch):
    v = vectors.Vector(vectors.ROOT / "skip.json", {"status": "frozen", "expect": {}})
    monkeypatch.setitem(BASELINE, v.id, True)
    monkeypatch.setitem(test_clause.__globals__, "context", lambda *a: {})

    def skip(*args):
        pytest.skip("L1.UNIT.003: another die")

    monkeypatch.setitem(test_clause.__globals__, "run", skip)
    try:
        with pytest.raises(pytest.skip.Exception, match="L1.UNIT.003"):
            test_clause(v, None, lambda *a: None)
        with pytest.raises(pytest.skip.Exception, match="L1.UNIT.003"):
            test_mutant_fails(v, "unused", None, lambda *a: None)
        BASELINE_SKIPS.pop(v.id)
        with pytest.raises(pytest.fail.Exception, match="fails unmutated"):
            test_mutant_fails(v, "unused", None, lambda *a: None)
    finally:
        BASELINE_SKIPS.pop(v.id, None)


def test_poisoned_poisons_debug_region_on_direct_queue_submit():
    mem = MemoryTransport()
    memory = CardMemory((0x10_0000, 0x40_0000), {0: (0x80_0000, 0x90_0000)})
    q = NodeQueue(mem, 0x80_0000)
    q.init()
    dbg_base, rec, cap = 0x20_0000, 0x480, 0x200
    dbg_size = rec + cap
    head = bytearray(RC.DBG_LASTS)
    struct.pack_into("<Q", head, 32, rec)
    struct.pack_into("<Q", head, 40, cap)
    mem.write_block(dbg_base, bytes(head) + bytes(dbg_size - RC.DBG_LASTS))
    card = SimpleNamespace(
        mem=mem,
        memory=memory,
        debug=True,
        nodes=(0,),
        queues={0: q},
        debug_regions={0: (dbg_base, dbg_size)},
        _order={},
    )
    fake_session = SimpleNamespace(dev=SimpleNamespace(card=card, private={}))
    # Witness: without the NodeQueue.submit hook (only NodeCard.submit hooked),
    # a direct q.submit (dbg.fail, dbg.session, _debug_rerun) leaves the debug
    # region zeroed.
    with (
        F.patched(F.QC, "NodeQueue", SimpleNamespace(submit=lambda *a, **kw: None)),
        poisoned(fake_session),
    ):
        pass
    assert mem.read_block(dbg_base + RC.DBG_LASTS, dbg_size - RC.DBG_LASTS) == bytes(
        dbg_size - RC.DBG_LASTS
    )
    # With the hook, q.submit poisons the debug region before the run starts.
    with poisoned(fake_session):
        q.submit(bytes(32))
    assert mem.read_block(dbg_base + RC.DBG_LASTS, dbg_size - RC.DBG_LASTS) == bytes(
        [F.POISON] * (dbg_size - RC.DBG_LASTS)
    )


def rust_mutant(name: str):
    """Enable the native mutant alongside any host patch.

    A native build without mutants is an error, never a measured kill.
    """
    carried = rs.mutants()
    if not carried:
        pytest.fail("this _rs build carries no mutants (build_rs.py spec)")
    return rs.mutant(name) if name in carried else contextlib.nullcontext()


FROZEN = [v for v in VECTORS if v["status"] == "frozen"]
WITNESSES = [(v, m) for v in FROZEN for m in v["mutants"]]


@pytest.mark.parametrize("v,name", WITNESSES, ids=[f"{v.id}~{m}" for v, m in WITNESSES])
def test_mutant_fails(v, name, session, record_property):
    ctx = context(v, session)
    if v.get("tier") == 2 and v.id not in BASELINE:
        test_clause(v, session, record_property)
    if v.id in BASELINE_SKIPS:
        pytest.skip(BASELINE_SKIPS[v.id])
    if BASELINE.get(v.id) is False:
        pytest.fail(f"{v.id} fails unmutated in this run: no kill is measured")
    routed = v["tier"] == 0 and v["runner"] in rs.spec_runners()
    if routed and name not in rs.mutants():
        pytest.fail(f"Rust runner {v['runner']} has no mutant {name}")
    with (
        fresh(session),
        over_budget(session, record_property),
        contextlib.nullcontext() if routed else MUTANTS[name](),
        rust_mutant(name),
    ):
        try:
            got = run(v, ctx)
        except (OverBudget, TransportUnavailable):
            # An unreachable card or model is no run of the mutant: no kill.
            raise
        except TimeoutError as e:
            # A host timeout is a kill only for a mutant the vector names as
            # one that hangs the card on this stack (its "hangs").
            if name not in v.hangs("rust"):
                raise AssertionError(
                    f"mutant {name} timed out on {v.id}, which is not a kill: {e}"
                ) from e
            record_property("broken_by", f"{type(e).__name__}: {str(e)[:300]}")
            return
        except Exception as e:  # noqa: BLE001 -- any other failure breaks the vector
            record_property("broken_by", f"{type(e).__name__}: {str(e)[:300]}")
            return
    assert got != ctx.get("expect", v["expect"]), f"mutant {name} does not break {v.id}"
