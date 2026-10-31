"""Producer-local fusion budgets; final template capacity is checked by compilation.

These estimates cut scheduler edges, not an already lowered kernel. They do not
claim to bound a consumer's total IMEM, L1, or MG/VC schedule.
"""

import os
from collections import Counter
from dataclasses import dataclass
from math import prod

from tinygrad.uop import Ops

from kohakutpu.application.ktpugrad.lower import ALU


@dataclass(frozen=True)
class ProducerCost:
    alu: int
    reads: int
    reductions: int
    intermediate_bytes: int

    @classmethod
    def of(cls, src, shape):
        nodes = src.toposort(
            gate=lambda u: u.op not in (Ops.STAGE, Ops.AFTER, Ops.PARAM)
        )
        return cls(
            sum(u.op in ALU for u in nodes),
            sum(u.op is Ops.INDEX for u in nodes),
            sum(u.op is Ops.REDUCE for u in nodes),
            prod(shape) * src.dtype.itemsize,
        )


@dataclass(frozen=True)
class FusionBudget:
    """Explicit producer cost knobs, in words/instructions and saved bytes."""

    max_alu: int = 32
    l1_words: int = 512
    imem_words: int = 1024
    tile_words: int = 32
    instructions_per_alu: int = 8
    min_saved_bytes: int = 0

    def __post_init__(self):
        if (
            min(
                self.max_alu,
                self.l1_words,
                self.imem_words,
                self.tile_words,
                self.instructions_per_alu,
            )
            <= 0
            or self.min_saved_bytes < 0
        ):
            raise ValueError(
                "positive fusion capacities and nonnegative saved bytes required"
            )

    @classmethod
    def environment(cls):
        return cls(
            **{
                name: int(os.environ.get(f"KTPU_FUSE_{name.upper()}", default))
                for name, default in vars(cls()).items()
            }
        )

    def cut(self, cost):
        if cost.reductions:
            return "producer_reduction"
        if cost.alu > self.max_alu:
            return "producer_alu"
        if (cost.reads + 1) * self.tile_words > self.l1_words:
            return "producer_l1_estimate"
        if cost.alu * self.instructions_per_alu > self.imem_words:
            return "producer_imem_estimate"
        if 2 * cost.intermediate_bytes < self.min_saved_bytes:
            return "small_intermediate"
        return None


DECISIONS = Counter()
