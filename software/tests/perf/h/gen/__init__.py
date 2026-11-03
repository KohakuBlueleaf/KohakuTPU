"""Round h's program generators. A generator is a module here with
`generate(sysnodes, board) -> Module`: the source of one module, every fn of it on
the given sysnodes, and each fn's `Spec`. `hrun.py --gen NAME` runs one.

A generator states what it cannot express today as BLOCKED(feature) in its
docstring, never as a fn that pretends to."""

import dataclasses
import importlib
from collections.abc import Callable

from h.ledger import Ledger


@dataclasses.dataclass(frozen=True)
class Spec:
    """A fn's outputs (painted with POISON every run, read back and checked) and
    its host reference: its inputs (with ``"epoch"``) to ``{output: wanted}``.

    `numeric`: outputs checked numerically (statistics, no threshold); every
    other output is compared word for word. `inputs`: epoch -> ``{param: host array}``, written before every
    run when `fresh`, once at epoch 0 otherwise; other inputs are seeded once.
    `ledger`: the fn's rings, its ``slots`` output holding the slot words.
    `diagnostic`: a bounded diagnostic, not a legal candidate. `triple`: the
    runner runs serial -> overlap -> serial, each group of `overlaps` (default:
    every sysnode) alone, together, then alone again; `owners`: output -> the
    sysnode whose block writes it (POISON wanted when it did not run).
    `issued`: the sysnodes whose packages the runner submits (default: every
    one; the others are compiled, never run). `expected`: an expected failure,
    sysnode -> text its fault must hold (the run's verdict is EXPECTED,
    `check.verdict`). `refused`: a code the compiler
    or the static checker must refuse the fn with; such a fn is never issued
    (`runner.admission`). `provenance`: (outputs, binds, epoch) -> a record of
    every retained mismatch decoded (`stamp.Gather`), kept as the row's
    ``provenance``."""

    outputs: tuple[str, ...]
    reference: Callable[[dict], dict] | None = None
    numeric: tuple[str, ...] = ()
    inputs: Callable[[int], dict] | None = None
    fresh: bool = True
    ledger: Ledger | None = None
    diagnostic: bool = False
    triple: bool = False
    overlaps: tuple[tuple[int, ...], ...] = ()
    owners: dict[str, int] = dataclasses.field(default_factory=dict)
    issued: tuple[int, ...] = ()
    expected: dict[int, str] = dataclasses.field(default_factory=dict)
    refused: str = ""
    provenance: Callable[[dict, dict, int], dict] | None = None


@dataclasses.dataclass(frozen=True)
class Module:
    source: str
    specs: dict[str, Spec]


def load(name: str, sysnodes: tuple[int, ...], board: str) -> Module:
    """Generator `name`'s module on `sysnodes` of `board`."""
    return importlib.import_module(f"{__name__}.{name}").generate(sysnodes, board)
