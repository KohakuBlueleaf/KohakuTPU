"""Spec-test vectors: one JSON file per case, named after its clause.

    tests/spec/vectors/<layer>/<area>/<CLAUSE.ID>.<case>.json

    {
      "clause": "ISA.VC.040",        the clause the case tests
      "tier": 0,                      0 host, 1 card value, 2 Verilator
      "runner": "isa.vector.encode",  a registered runner
      "machine": "multimesh_v9t6",    the machine file it targets (optional)
      "status": "frozen",             frozen: passes now; ruling: passes once
                                      the stack follows the ruling (RUL.nnn)
      "input": {...},
      "expect": {...},
      "mutants": ["..."]              mutants the case must fail under
      "hangs": ["..."]                (optional) those of them whose kill is a
                                      host timeout: the mutant hangs the card;
                                      or {"python": [...], "rust": [...]}, per
                                      stack (`STACKS`) that runs the vector
    }

The same files drive the Python harness and the Rust stage tests.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "vectors"
CLAUSE = re.compile(r"^[A-Z0-9]+(\.[A-Z0-9]+)+$")
FIELDS = {"clause", "tier", "runner", "status", "input", "expect", "mutants"}
#: Frozen metadata keys; execution consumes only the Rust timeout set.
STACKS = ("python", "rust")


@dataclass(frozen=True)
class Vector:
    path: Path
    data: dict

    @property
    def id(self) -> str:
        return self.path.relative_to(ROOT).as_posix().removesuffix(".json")

    def __getitem__(self, key):
        return self.data[key]

    def get(self, key, default=None):
        return self.data.get(key, default)

    def hangs(self, stack: str) -> set:
        """The mutants whose kill is a host timeout on `stack`."""
        h = self.data.get("hangs", ())
        return set(h.get(stack, ()) if isinstance(h, dict) else h)


def check(v: Vector) -> list[str]:
    """What is wrong with a vector's shape (empty when it is well formed)."""
    bad = [f"missing {k}" for k in sorted(FIELDS - set(v.data))]
    if not CLAUSE.match(str(v.data.get("clause", ""))):
        bad.append(f"clause {v.data.get('clause')!r}")
    if not v.path.name.startswith(str(v.data.get("clause", "")) + "."):
        bad.append("file name does not start with the clause id")
    if v.data.get("status") not in ("frozen", "ruling"):
        bad.append(f"status {v.data.get('status')!r}")
    if v.data.get("tier") not in (0, 1, 2):
        bad.append(f"tier {v.data.get('tier')!r}")
    if v.data.get("status") == "frozen" and not v.data.get("mutants"):
        bad.append("a frozen vector names no mutant")
    h = v.data.get("hangs", ())
    if isinstance(h, dict) and set(h) - set(STACKS):
        bad.append(f"hangs keys {sorted(set(h) - set(STACKS))}: not of {STACKS}")
    named = set().union(*(v.hangs(s) for s in STACKS))
    stray = named - set(v.data.get("mutants", ()))
    if stray:
        bad.append(f"hangs names mutants it does not list: {sorted(stray)}")
    return bad


def load(root: Path = ROOT) -> list[Vector]:
    return [
        Vector(p, json.loads(p.read_text(encoding="utf-8")))
        for p in sorted(root.rglob("*.json"))
    ]


__all__ = ["ROOT", "STACKS", "Vector", "check", "load"]
