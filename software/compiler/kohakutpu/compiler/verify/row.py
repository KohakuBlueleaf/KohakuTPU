"""A Streaming Row Unit on the abstract machine (`sru_cu.v`).

The shell runs its words in order, one at a time; every word is one
completion. CFG, LOAD and STORE touch only the unit and memory. A RUN holds
the shell until the job is done and its last output left:

* A from a COUNT stream (`amem` 0): the unit posts its producer min(the A
  FIFO's depth, the stream's words) at the RUN and min(8, the rest) for every
  8 words it takes; the words come by bursts into buffer 0, counted (`rx`).
* Out to a unit (`peer`): bursts of at most 8 words, never across a sub-row,
  each leaving once the credits hold its length, into the sink's buffer
  `pbuf` from granule `poff` on.

A sub-row's output is taken as ready once all its input is in (the buffered
form; a single-pass job is earlier on the card, which only relaxes the
waits), and input runs at most two sub-rows (the buffer's halves) plus the A
FIFO ahead of the output that has left.
"""

from kohakutpu.compiler.isa import sru as S
from kohakutpu.compiler.verify.model import Port, Proc

#: The A FIFO's depth (sru_cu.v FIFO_DEPTH): the credits a RUN posts first.
FIFO = 64
BURST = 8
#: Sub-rows the core holds past the output that has left (the buffer's halves).
HELD = 2


class Row:
    kind = "SR"

    def __init__(self, card, key: tuple, name: str) -> None:
        self.card, self.key, self.name = card, key, name
        m = card.m
        self.words: list = []
        self.rx = m.counter(f"rx:{name}")
        self.taken = 0
        self.cr = m.counter(f"cr:{name}")
        self.spent = 0
        self.done = m.counter(f"done:{name}")
        self.recv = m.counter(f"recv:{name}")
        self.acks = m.counter(f"acks:{name}")
        #: the configuration words the shell reads (sru_cu.v cfg[0], cfg[1])
        self.cfg = {0: 0, 1: 0}
        self.port = Port(m, name)
        self.seq = _Shell(self)
        self.completer = self.seq.ix
        self.landings: list = []

    def deliver(self, word: int, clock) -> None:
        self.words.append((word, clock))

    def land(
        self, buf: int, off: int, n: int, count: bool, clock, what: str, sent=None
    ) -> str:
        """`n` words into the A FIFO (buffer 0, any offset: the FIFO takes
        them in arrival order)."""
        if buf != 0:
            return f"buffer {buf}: a row unit takes only buffer 0 (A)"
        if count:
            self.rx.add(n, clock, self.port.ix)
        return ""

    def joins(self, buf: int) -> list:
        return []

    def credit(self, n: int, clock, owner: int) -> None:
        self.cr.add(n, clock, owner)

    def rest(self) -> list:
        out = []
        if self.rx.value != self.taken:
            out.append(f"{self.rx.value - self.taken} words landed and never taken")
        if self.cr.value != self.spent:
            out.append(f"{self.cr.value - self.spent} credits received and never spent")
        return out

    def field(self, name: str) -> int:
        a, lo, w = S.G[name]
        return (self.cfg.get(a, 0) >> lo) & ((1 << w) - 1)


class _Shell(Proc):
    def __init__(self, u: Row) -> None:
        super().__init__(u.card.m)
        self.u = u
        self.name = f"{u.name} shell"
        self.pc = 0
        self.job = None
        self.why = ""

    def done(self) -> bool:
        return (
            self.pc >= len(self.u.words)
            and self.job is None
            and self.u.card.node_done(self.u)
        )

    def blocked(self) -> str:
        if self.job is not None:
            return f"RUN: {self.why}"
        return "waiting for words from the node"

    def step(self) -> bool:
        u = self.u
        if self.job is not None:
            return self.job.step()
        if self.pc >= len(u.words):
            return False
        word, given = u.words[self.pc]
        fmt = S.ISA.find(word)
        self.pc += 1
        clock = self.tick(given)
        if fmt is None:
            u.card.m.find(
                "model", self.name, "a word decodes as no row unit instruction"
            )
        else:
            f = fmt.decode(word)
            if fmt.name == "CFG" and f["word"] in u.cfg:
                u.cfg[f["word"]] = f["value"]
            elif fmt.name == "POST":
                u.card.post(u, (f["x"], f["y"]), f["n"], clock, self.ix)
            elif fmt.name == "RUN":
                self.job = _Job(self, f, clock)
                return True
        self.retire(clock)
        return True

    def retire(self, clock) -> None:
        self.u.done.add(1, clock, self.ix)
        self.u.recv.add(1, clock, self.ix)


class _Job:
    """One RUN: input words taken, output words sent, in the shell's events."""

    def __init__(self, sh: _Shell, f: dict, clock) -> None:
        u = sh.u
        self.sh, self.f = sh, f
        self.nw, self.nsr = u.field("NW"), u.field("NSR")
        out = u.field("OUT")
        self.onw = 0 if not out else (self.nw if out == 1 else self.nw // 2)
        self.stream = not f["amem"]
        self.peer = bool(f["peer"]) and self.onw > 0
        self.total = self.nw * self.nsr
        self.inn = 0 if self.stream else self.total
        self.out = 0  # output words sent
        #: stream words the producer is still to be posted (RUN `words`)
        self.cr_left = f["words"]
        self.clock = clock
        if self.stream:
            if f["words"] != self.total:
                u.card.m.find(
                    "model",
                    sh.name,
                    f"RUN words={f['words']}, the job streams {self.total}",
                )
            self._post(FIFO, clock)

    def _post(self, n: int, clock) -> None:
        """min(n, what is left) to the producer: the posts sum to the stream."""
        n = min(n, self.cr_left)
        if n > 0:
            sh = self.sh
            sh.u.card.post(sh.u, (self.f["ax"], self.f["ay"]), n, clock, sh.ix)
            self.cr_left -= n

    def rows_in(self) -> int:
        return self.inn // self.nw if self.nw else self.nsr

    def step(self) -> bool:
        sh, u = self.sh, self.sh.u
        if self.stream and self.inn < self.total:
            rows_out = self.out // self.onw if self.peer else self.nsr
            room = (rows_out + HELD) * self.nw + FIFO - self.inn
            have = u.rx.value - u.taken
            n = min(have, room, self.total - self.inn)
            if n > 0:
                clock = sh.tick(self.clock, u.rx.clock_to(u.taken + n))
                before = self.inn // BURST
                self.inn += n
                u.taken += n
                for _ in range(self.inn // BURST - before):
                    self._post(BURST, clock)
                self.clock = clock
                return True
        if self.peer and self.out < self.onw * self.nsr:
            ready = self.rows_in() * self.onw
            end = (self.out // self.onw + 1) * self.onw
            n = min(BURST, end - self.out)
            if self.out + n <= ready and u.cr.value - u.spent >= n:
                clock = sh.tick(self.clock, u.cr.clock_to(u.spent + n))
                u.spent += n
                f = self.f
                sink = {
                    "to": (f["px"], f["py"]),
                    "far": None,
                    "mesh": u.key[0],
                    "buf": f["pbuf"],
                    "count": bool(f["count"]),
                    "signal": False,
                    "ack": (0, 0),
                }
                u.card.send(u, sink, (f["poff"] + self.out) & 0xFFFF, n, clock, sh.ix)
                self.out += n
                self.clock = clock
                return True
        if self.inn >= self.total and (
            not self.peer or self.out >= self.onw * self.nsr
        ):
            sh.job = None
            sh.retire(sh.tick(self.clock))
            return True
        sh.why = (
            f"{self.inn} of {self.total} words in ({u.rx.value - u.taken} landed), "
            f"{self.out} out, credits {u.cr.value - u.spent}"
        )
        return False


__all__ = ["Row"]
