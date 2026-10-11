"""What the language's lowerings and optimisers ask of a machine.

The language holds no machine: a compiler hands its `Target` to every stage
that needs one (`pipeline`), so the same passes run against any machine that
answers these questions.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Target:
    name: str
    #: Unit kind (``VC``, ``MG``) -> how many the machine has.
    units: dict
    #: A vector core's lanes, register chunks, L1 words and registers.
    lanes: int
    chunks: int
    l1_words: int
    registers: int
    #: Cycles from a vector op's last beat to a dependent op's first.
    vector_latency: int


__all__ = ["Target"]
