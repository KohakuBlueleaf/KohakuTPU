"""The lower seam: what a project's compute unit tells the compiler.

Only :meth:`Backend.encode` is required; the rest read the task's own fields.
"""

import abc
from dataclasses import dataclass

from kohakuaccel.ir.l2 import Coord, Policy, Task


@dataclass(frozen=True)
class EncodeContext:
    """Where and how this task is being dispatched.

    `peers` is the other coordinates that could share this task's fetch and
    `leads` says whether this task still issues one. Honouring one without the
    other delivers the data twice.
    """

    coord: Coord
    slot: int
    peers: tuple[Coord, ...] = ()
    leads: bool = True


class Backend(abc.ABC):
    """How the framework turns one project's tasks into flits."""

    @abc.abstractmethod
    def encode(self, task: Task, ctx: EncodeContext) -> list[int]:
        """The instruction flits for `task`.

        Must return exactly :meth:`flits` of them: round packing and the staging
        layout were both computed from that number before this ran.
        """

    def flits(self, task: Task) -> int:
        """How many instruction flits `task` occupies."""
        return task.flits

    def signals(self, task: Task) -> int:
        """How many completion signals `task` produces."""
        return task.signals

    def cost(self, task: Task) -> float:
        """A relative weight, used only to balance placement."""
        return task.cost

    def policy(self, task: Task) -> Policy:
        """Which units `task` may be placed on."""
        return task.policy
