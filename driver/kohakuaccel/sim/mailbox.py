"""A node's dispatch mailbox over a `SimMachine`: send a payload, get completions.

The `Mailbox` of `kohakuaccel.package.interp`, answered by the in-process unit
models; a signal carrying an `at` is reported from that coordinate.
"""

from kohakuaccel.device.flit import header
from kohakuaccel.device.registers import T_CU_INST


class SimMailbox:
    def __init__(self, machine) -> None:
        self.machine = machine
        self.queue: list[tuple[int, int, int, int]] = []
        self.sent = 0
        self.txn = 0

    def send(self, x: int, y: int, payload: int) -> None:
        unit = self.machine.units.get((x, y))
        if unit is None:
            raise KeyError(f"no unit at ({x},{y}), so nothing will complete")
        self.txn = (self.txn + 1) & 0xFF
        flit = header(T_CU_INST, self.txn, True) | payload
        for sig in unit.execute(flit, self.machine.mem):
            at = getattr(sig, "at", None) or (x, y)
            self.queue.append((at[0], at[1], sig.code, sig.arg))
        self.sent += 1

    def drain(self) -> list[tuple[int, int, int, int]]:
        out, self.queue = self.queue, []
        return out
