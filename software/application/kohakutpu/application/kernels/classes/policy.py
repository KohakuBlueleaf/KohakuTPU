"""Compile-time shape classes; dimensions and limits are supplied by the machine."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ShapeClass:
    kblock: int
    max_block: int
    policy: str = "pow2"
    cores: str = "all"

    def __post_init__(self):
        if self.policy not in ("exact", "pow2", "coarse"):
            raise ValueError("unknown width class policy")
        if self.cores not in ("all", "min"):
            raise ValueError("core policy is all or min")
        if (
            self.kblock <= 0
            or self.kblock & (self.kblock - 1)
            or self.max_block < self.kblock
            or self.max_block & (self.max_block - 1)
        ):
            raise ValueError("class limits must be ordered powers of two")

    def width(self, width):
        if width <= 0 or width % self.kblock:
            raise ValueError("row width must contain whole machine column blocks")
        if width > self.max_block:
            raise ValueError("wide class needs a column-block loop")
        if self.policy == "exact":
            return width
        if self.policy == "coarse":
            return (
                max(self.kblock, self.max_block // 2)
                if width <= self.max_block // 2
                else self.max_block
            )
        return max(self.kblock, 1 << (width - 1).bit_length())
