"""The framework's program builder (`kohakuaccel.compiler.program`) with no
project: a generic machine, opaque one-word ops, and a toy lowering hook."""

from dataclasses import dataclass
from typing import ClassVar

from kohakuaccel.compiler.machine import MachineSpec
from kohakuaccel.compiler.package.format import Op
from kohakuaccel.compiler.program import Program

MACHINE = MachineSpec(
    name="tiny", units={"UA": ((1, 0), (2, 0)), "UB": ((3, 0),)}, inst_depth=64
)


@dataclass(frozen=True)
class Word:
    value: int

    def flits(self) -> list[int]:
        return [self.value]


def steps(prog, resident=None):
    return prog.build(None, resident).build(defaults=False).steps


def test_sends_between_syncs_are_one_dispatch_per_unit_and_waits_count_words():
    p = Program(MACHINE)
    a, b = p.units("UA")
    p.send(a, Word(1)).send(b, Word(2)).send(a, Word(3), Word(4)).wait(a)
    p.send(a, Word(5)).barrier()
    got = [(s.op, s.count) for s in steps(p) if s.op in (Op.DISPATCH, Op.AWAIT)]
    sends = [(Op.DISPATCH, 3), (Op.DISPATCH, 1), (Op.AWAIT, 3)]
    assert got == sends + [(Op.DISPATCH, 1), (Op.AWAIT, 1), (Op.AWAIT, 1)]


def test_a_stream_past_its_credit_interleaves_in_credit_sized_steps():
    """The node sends a fetched step whole, waiting on that unit's credit: a
    stream longer than the credit (64 here) is cut at it and the units' pieces
    alternate, each unit's words in order; a stream within it stays whole."""
    p = Program(MACHINE)
    a, b = p.units("UA")
    p.send(a, *[Word(k) for k in range(150)]).send(
        b, *[Word(1000 + k) for k in range(100)]
    )
    p.barrier()
    p.send(a, *[Word(k) for k in range(40)]).send(b, *[Word(k) for k in range(30)])
    pkg = p.build(None, None).build(defaults=False)
    units = [(u.x, u.y) for u in pkg.units]
    got = [(units[s.unit], s.count) for s in pkg.steps if s.op == Op.DISPATCH]
    assert got == [(a, 64), (b, 64), (a, 64), (b, 36), (a, 22), (a, 40), (b, 30)]
    first = pkg.steps[: next(i for i, s in enumerate(pkg.steps) if s.op == Op.BARRIER)]
    words = {u: [] for u in units}
    for s in first:
        if s.op == Op.DISPATCH:
            words[units[s.unit]] += pkg.payloads[s.arg : s.arg + s.count]
    assert words[a] == list(range(150)) and words[b] == [1000 + k for k in range(100)]


def test_a_wait_up_to_a_mark_awaits_only_the_words_before_it():
    """The unit keeps the words sent after the mark: they are dispatched before
    the AWAIT, and the final barrier awaits the rest."""
    p = Program(MACHINE)
    a, b = p.units("UA")
    p.send(a, Word(1), Word(2))
    tok = p.mark(a)
    p.send(a, Word(3), Word(4), Word(5)).send(b, Word(6))
    p.wait(a, tok).send(b, Word(7))
    got = [(s.op, s.count) for s in steps(p) if s.op in (Op.DISPATCH, Op.AWAIT)]
    assert got == [
        (Op.DISPATCH, 2),  # the mark sends a's words so far
        (Op.DISPATCH, 3),
        (Op.DISPATCH, 1),
        (Op.AWAIT, 2),  # up to the mark only
        (Op.DISPATCH, 1),
        (Op.AWAIT, 3),  # the rest of a, at the end
        (Op.AWAIT, 2),
    ]


def test_a_token_names_its_own_unit():
    p = Program(MACHINE)
    a, b = p.units("UA")
    tok = p.mark(a)
    try:
        p.wait(b, tok)
    except ValueError:
        return
    raise AssertionError("a wait on b accepted a's mark")


def _held(words: list, state: dict, coord) -> list:
    """Drop the words this unit already holds."""
    held = state.setdefault(coord, set())
    out = [w for w in words if w not in held]
    held.update(out)
    return out


class Dropping(Program):
    lowerings: ClassVar[dict] = {"UB": _held}


def test_a_lowering_hook_runs_per_unit_type_against_state_kept_across_packages():
    p = Dropping(MACHINE)
    (w,) = p.units("UB")
    u, _ = p.units("UA")
    p.send(w, Word(7), Word(8)).send(u, Word(7)).barrier()
    state: dict = {}
    first = sorted(s.count for s in steps(p, state) if s.op == Op.DISPATCH)
    again = [s.count for s in steps(p, state) if s.op == Op.DISPATCH]
    plain = sorted(s.count for s in steps(p) if s.op == Op.DISPATCH)
    assert first == [1, 2] and again == [1], "UB's words are held; UA's are not"
    assert plain == [1, 2], "without state no hook runs"
