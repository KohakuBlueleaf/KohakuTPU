"""What the round h generators share: the module head, and a fn body of `node N`
blocks whose rings are counted, so a fn that waits bells takes every bell it was
rung (the firmware counts bells since boot, slots persist across loads: F-007),
and a fn with a ledger reads back every slot it was rung on."""

import collections
import dataclasses

import numpy as np

from h import check
from h.ledger import Ledger
from kohakutpu.application.tools.l1 import POISON

HEAD = "target ktpu.v9\n"

#: The most bells one `wait bell` takes: half the 16-bit count.
BELL_TAKE = 1 << 15
#: The registers a ledger read and an observation use.
SLOT_REG, SEEN_REG = "r14", "r5"


@dataclasses.dataclass
class Body:
    """One fn's `node N` blocks; `bells`: each block ends taking the bells it was
    rung and did not wait (a module that waits bells needs every fn to);
    `ledger`: each block ends storing the signal word of every slot it was rung
    on into a ``slots`` output (`Ledger.slots` order)."""

    bells: bool = False
    ledger: bool = False
    lines: dict = dataclasses.field(
        default_factory=lambda: collections.defaultdict(list)
    )
    rung: collections.Counter = dataclasses.field(default_factory=collections.Counter)
    belled: collections.Counter = dataclasses.field(default_factory=collections.Counter)
    #: (source, dest, slot, amount) -> packets
    sent: collections.Counter = dataclasses.field(default_factory=collections.Counter)

    def add(self, n: int, *lines: str) -> None:
        self.lines[n].extend(lines)

    def ring(
        self,
        src: int,
        dst: int,
        slot: int,
        amount: int = 1,
        plain: bool = False,
        times: int = 1,
    ) -> None:
        """`src` rings `dst`'s `slot`; `times`: how often the line runs (a loop)."""
        flag = " plain" if plain else ""
        self.add(src, f"ring mesh={dst} slot={slot} amount={amount}{flag}")
        self.rung[dst, src] += times
        self.sent[src, dst, slot, amount] += times

    def take(self, n: int, slot: int, count: int) -> None:
        self.add(n, f"wait slot {slot} n={count} consume")

    def bell(self, n: int, src: int, count: int, times: int = 1) -> None:
        self.add(n, f"wait bell {src} n={count}")
        self.belled[n, src] += count * times

    def barrier(self, sysnodes, slot: int) -> None:
        """A start barrier: the first sysnode gathers one ring from each other and
        rings each back; every block leaves it together."""
        lead, *rest = sysnodes
        for n in rest:
            self.ring(n, lead, slot)
        if rest:
            self.take(lead, slot, len(rest))
        for n in rest:
            self.ring(lead, n, slot)
            self.take(n, slot, 1)

    def stores(self, n: int, at: str, offsets, epoch: int, producer: int, region: int):
        """`n`'s CPU stores of the tag words of `offsets` (words) at `at`."""
        for o in offsets:
            self.add(
                n, f"store {at} + {8 * o} <- {check.tag(epoch, producer, region, o)}"
            )

    def observe(self, n: int, src: str, offsets, dst: str, first: int) -> None:
        """`n` copies the words `offsets` of `src` to `dst` words `first`.., by
        CPU loads right after its acquisition (before any ack)."""
        for k, o in enumerate(offsets):
            self.add(
                n,
                f"{SEEN_REG} = load {src} + {8 * o}",
                f"store {dst} + {8 * (first + k)} <- {SEEN_REG}",
            )

    def slots(self) -> tuple[tuple[int, int], ...]:
        return tuple(sorted({(d, slot) for _, d, slot, _ in self.sent}))

    def ledger_of(self) -> Ledger:
        rings = tuple(
            (s, d, slot, amount, packets)
            for (s, d, slot, amount), packets in sorted(self.sent.items())
        )
        return Ledger(rings, self.slots())

    def fn(self, name: str, params: str) -> str:
        if self.bells:
            for (dst, src), rings in sorted(self.rung.items()):
                left = rings - self.belled[dst, src]
                while left:
                    k = min(left, BELL_TAKE)
                    self.bell(dst, src, k)
                    left -= k
        if self.ledger:
            for k, (n, slot) in enumerate(self.slots()):
                self.add(
                    n,
                    f"ctrl il_select <- {slot}",
                    f"{SLOT_REG} = ctrl sig",
                    f"store slots + {8 * k} <- {SLOT_REG}",
                )
            params += f", slots: u64[{len(self.slots())}] @dram"
        text = f"\nfn {name}.l1({params})"
        for n in sorted(self.lines):
            text += f"\n    node {n}" + "".join(f"\n        {s}" for s in self.lines[n])
        return text + "\n"


def hops(a: int, b: int) -> int:
    """Interlink hops between two dies on the line."""
    return abs(a - b)


def nearest(n: int, sysnodes) -> list[int]:
    """The other sysnodes, nearest first."""
    return sorted((m for m in sysnodes if m != n), key=lambda m: (hops(n, m), m))


def counts(k: int, n: int) -> dict:
    """A `count` output of `n` sysnodes' round counts, each `k`."""
    return {"count": np.full(n, k, np.uint64)}


def poison(dtype, n: int) -> np.ndarray:
    """`n` values of `dtype` whose bytes are all POISON (an output left unwritten)."""
    return np.frombuffer(bytes([POISON]) * (n * np.dtype(dtype).itemsize), dtype).copy()


def host_tags(params: dict):
    """An `inputs` callable: each ``{param: (nbytes, dtype, region)}`` filled with
    host tag words of the run's epoch."""

    def inputs(epoch: int) -> dict:
        return {
            p: check.tagged(epoch, check.HOST, region, nbytes, dtype)
            for p, (nbytes, dtype, region) in params.items()
        }

    return inputs
