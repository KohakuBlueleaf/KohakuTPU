"""The active mutation switch, one for the tracer and the Rust stages
(`kohakutpu.stack.rs.mutant` sets it; `_rs.compile(mutant=)` takes it)."""

import contextvars

MUTANT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "rs_mutant", default=None
)
#: The index printers leave a sum that starts with an atom unbracketed inside
#: another atom (ktpu-graph `mutant::ATOM_SUM_SHORTCUT`).
ATOM_SUM_SHORTCUT = "graph.text/atom-sum-shortcut"
