"""A debug session (docs/spec/stack/debug/firmware.md s2.4): the debug image
booted once per node, many small runs, each failure recorded and the session
going on, under one seconds budget.

    with DebugSession(card, seconds=60) as s:
        s.run(program)                       # a card.run, recorded
        s.attempt(lambda: L1.run(card, "memops", {}, reps=1))
    s.records                                # one per run, in order

A failed run is re-run once off the dispatch engine and traced (RUL.075,
`NodeCard.run`); the firmware is restarted only when the failure did not
drain (RUL.082). A FATAL firmware ends the session at once (DBG.SESS.004).
"""

from typing import Self

from kohakuaccel.driver.node.boot.args import bound
from kohakuaccel.driver.node.queue import FirmwareFatal, NodeError


class SessionFatal(RuntimeError):
    """The firmware stopped fatally (DBG.SESS.004): its line's state and code,
    the stdout ring's text, and each node's console tap."""

    def __init__(self, state: dict, stdout: str, consoles: dict) -> None:
        self.state, self.stdout, self.consoles = state, stdout, consoles
        taps = "".join(
            f"\nnode {n} console:\n{c.rstrip()}" for n, c in consoles.items()
        )
        super().__init__(
            f"DBG.SESS.004: firmware fatal, code {state['fatal']:#x}"
            f"\nnode said:\n{stdout.rstrip()}{taps}"
        )


class SessionBudget(TimeoutError):
    """No time left for another run within the session's budget, past the
    firmware's own wait bound (DBG.SESS.003, DBG.SESS.005)."""


class DebugSession:
    """`card` (a `NodeCard`) for one budget of `seconds`. Each wait the
    firmware makes is bounded by `wait` seconds at the card's sysnode clock
    (RUL.033: the session's own budget), and each run's host wait is the time
    left, which must exceed `wait` so a hang ends as the firmware's TIMEOUT
    with its report, never as the host's (DBG.SESS.003). The budget counts the
    card's clock (`clock` by default), the one its waits count."""

    def __init__(
        self, card, seconds: float = 60.0, wait: float | None = None, clock=None
    ) -> None:
        self.card = card
        self.seconds = seconds
        self.wait = seconds / 4 if wait is None else wait
        if not 0 < self.wait < seconds:
            raise ValueError(
                f"DBG.SESS.003: a firmware wait of {self.wait} s within {seconds} s"
            )
        self.clock = clock or card.clock
        #: One record per run: ``{"ok", "result"}`` or ``{"ok", "error",
        #: "completion", "debug", "rerun", "dump", "drained"}``.
        self.records: list[dict] = []

    # ------------------------------------------------------------ the session
    def __enter__(self) -> Self:
        self.t0 = self.clock()
        card = self.card
        self._keep = (card.timeout, {n: a.timeout for n, (_, a) in card.boots.items()})
        cycles = bound(self.wait, card.sys_hz / 1e6)
        for _, args in card.boots.values():
            args.timeout = cycles
        card.load(debug=True)
        return self

    def __exit__(self, kind, err, tb) -> None:
        card = self.card
        card.timeout, timeouts = self._keep
        for n, (_, args) in card.boots.items():
            args.timeout = timeouts[n]
        # A fatal firmware is left as it stopped, for the host to look at.
        if not isinstance(err, SessionFatal):
            card.load(debug=False)

    def left(self) -> float:
        """Seconds of the budget not yet spent."""
        return self.seconds - (self.clock() - self.t0)

    # ------------------------------------------------------------------ runs
    def run(self, program, **kw) -> dict:
        """`card.run(program, **kw)` as :meth:`attempt` records it."""
        return self.attempt(lambda: self.card.run(program, **kw))

    def attempt(self, fn) -> dict:
        """`fn()` within the budget: its result recorded, or its failure
        (`NodeError`, with the debug report read now when `fn` did not) and
        the session goes on. Raises :class:`SessionBudget` when the time left
        does not exceed the firmware's wait, :class:`SessionFatal` on a FATAL
        firmware."""
        left = self.left()
        if left <= self.wait:
            raise SessionBudget(
                f"DBG.SESS.005: {left:.3f} s left of {self.seconds} s, "
                f"the firmware's wait is {self.wait} s"
            )
        card = self.card
        card.timeout = left
        try:
            rec = {"ok": True, "result": fn()}
        except NodeError as e:
            if e.debug is None and card.debug:
                e.debug = card.debug_report()
            rec = {
                "ok": False,
                "error": str(e),
                "completion": e.completion,
                "debug": e.debug,
                "rerun": e.rerun,
                "dump": e.dump,
                "drained": card.drained(e.debug, e.console),
            }
        except FirmwareFatal as e:
            consoles = {n: loader.console() for n, (loader, _) in card.boots.items()}
            raise SessionFatal(e.state, e.console, consoles) from e
        self.records.append(rec)
        return rec


__all__ = ["DebugSession", "SessionBudget", "SessionFatal"]
