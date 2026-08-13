"""What the framework knows about a machine, and how a project extends it.

``ktpu.target.Target`` carried both halves at once: `stage_flits`, `ncmd` and
`mhz` are dispatch limits any KohakuAccel machine has, while `clusters`,
`tiles`, `l1_a`, `lanes`, `kblock` and `vector_cores` are all one project's unit
shapes. The feature mechanism was the sharpest case -- a genuinely good
framework pattern whose vocabulary was entirely KohakuTPU's.

So the mechanism lives here and the vocabulary does not: a project subclasses
:class:`Target`, declares its own ``FEATURES``, and adds its capacities.
"""

from dataclasses import dataclass, field
from typing import ClassVar


@dataclass(frozen=True)
class Target:
    """A machine configuration: dispatch limits, clock, and optional features.

    Describes what the elaborated mesh HAS, not what a program chooses to use.

    `stage_flits` bounds one dispatch round -- work is admitted to a round until
    its flits exceed it, so many small passes serialise into many rounds.
    `ncmd` is the per-round command RAM depth.

    `features` names optional hardware a bitstream either has or does not.
    Everything in it must appear in this class's ``FEATURES``, which a subclass
    declares; a program built for a machine without a feature must still lower
    and still run, so a feature may gate what codegen EMITS and never what it
    assumes.
    """

    #: The feature vocabulary this machine family defines. A project overrides.
    FEATURES: ClassVar[tuple[str, ...]] = ()

    name: str = "generic"

    stage_flits: int = 128
    ncmd: int = 128
    mhz: float = 300.0

    features: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        """Reject an unknown feature at construction rather than at query time.

        A misspelled feature must not read as absent: that silently produces the
        slow path forever and looks like the optimisation never worked. Checking
        here catches it even when nothing ever calls :meth:`has`.
        """
        unknown = set(self.features) - set(self.FEATURES)
        if unknown:
            raise ValueError(
                f"{type(self).__name__} has no feature(s) {sorted(unknown)}; "
                f"declared are {list(self.FEATURES)}"
            )

    def has(self, feature: str) -> bool:
        """True if this machine implements `feature`.

        Raises :class:`ValueError` on a name this family never declared.
        """
        if feature not in self.FEATURES:
            raise ValueError(
                f"unknown feature {feature!r}; {type(self).__name__} declares "
                f"{list(self.FEATURES)}"
            )
        return feature in self.features
