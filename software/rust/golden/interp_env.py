"""The world the package interpreter's frozen witness runs it in
(`bindings/interp.json`): a mailbox that completes every payload at once, a
mover, memory, store, control registers and an RX queue that answer
deterministically, and a log of everything the interpreter does through them.
`bindings_dump.py` froze the logs with the Python package reader;
`test_rs_bindings.py` holds the reader the interpreter runs on now to them.
"""

import hashlib
import json

#: Each buffer's binding: buffer i at `BIND * (i + 1)`.
BIND = 0x200000
#: Callbacks a run may make: a package that loops on what this world answers
#: (a JUMP polling a register it never sets) ends at the same entry under
#: either reader.
LOG_MAX = 5_000
#: Bytes one memory read may ask for (a FETCH of a count no real package
#: holds ends the run, not the host).
MEMORY_MAX = 1 << 20


class Budget(Exception):
    """A run made `LOG_MAX` callbacks."""


class World:
    """Every callback the interpreter takes, each call logged in order."""

    def __init__(self) -> None:
        self.log: list = []
        self.pending: list = []

    def _logged(self, entry: list) -> None:
        self.log.append(entry)
        if len(self.log) >= LOG_MAX:
            raise Budget

    # Mailbox
    def send(self, x: int, y: int, payload: int) -> None:
        self._logged(["send", x, y, hex(payload)])
        self.pending.append((x, y))

    def drain(self) -> list:
        out = [(x, y, 0, 0) for x, y in self.pending]
        self.pending = []
        return out

    def mover(self, writes) -> None:
        self._logged(["mover", [[r, hex(v)] for r, v in writes]])

    def memory(self, addr: int, nbytes: int) -> bytes:
        self._logged(["memory", hex(addr), nbytes])
        if nbytes > MEMORY_MAX:
            raise Budget
        return bytes((addr + k) * 7 & 0xFF for k in range(nbytes))

    def store(self, addr: int, value: int) -> None:
        self._logged(["store", hex(addr), hex(value)])

    def ctrl(self, offset: int) -> int:
        self._logged(["ctrl", hex(offset)])
        return offset ^ 0x5A5A

    def rx(self) -> tuple:
        self._logged(["rx"])
        return 0, 0, 0xC0FFEE

    def callbacks(self) -> dict:
        return {
            "mover": self.mover,
            "memory": self.memory,
            "store": self.store,
            "ctrl": self.ctrl,
            "rx": self.rx,
        }


def bindings(nbuffers: int) -> list:
    return [BIND * (i + 1) for i in range(nbuffers)]


def run(world: World, interp, raw: bytes, bindings: list) -> dict:
    """`interp.run` of `raw` in `world` as the witness keeps it; a run that
    reaches `LOG_MAX` or `MEMORY_MAX` as ``{"budget", "log"}``."""
    try:
        res = interp.run(raw, bindings, max_polls=64)
    except Budget:
        return {"budget": [LOG_MAX, MEMORY_MAX], "log": digest(world.log)}
    return result(world, res)


def result(world: World, res) -> dict:
    """The interpreter's run as the witness keeps it: the world's log and the
    result's every field, long lists as their digest."""
    out = {
        "status": res.status,
        "detail": res.detail,
        "step": res.step,
        "value": hex(res.value),
        "sent": res.sent,
        "signals": [[n, c, hex(a)] for n, c, a in res.signals],
        "regs": [hex(r) for r in res.regs],
        "events": [[*e[:4], hex(e[4])] for e in res.events],
        "flits": [[x, y, k, hex(w)] for x, y, k, w in res.flits],
        "log": world.log,
    }
    return {k: digest(v) if isinstance(v, list) else v for k, v in out.items()}


def digest(xs: list) -> object:
    """`xs`, or past 16 entries its canonical JSON's sha256 and length."""
    if len(xs) <= 16:
        return xs
    text = json.dumps(xs, separators=(",", ":"))
    return {"sha256": hashlib.sha256(text.encode()).hexdigest(), "len": len(xs)}
