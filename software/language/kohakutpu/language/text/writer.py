"""Indented text, a line at a time: what every writer and lowering of the
language builds its output with."""

from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class Text:
    lines: list = field(default_factory=list)
    depth: int = 0

    def __call__(self, line: str) -> None:
        self.lines.append("    " * self.depth + line)

    @contextmanager
    def block(self):
        """Lines written inside are one level deeper."""
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


__all__ = ["Text"]
