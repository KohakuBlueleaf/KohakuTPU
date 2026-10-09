"""L3, the tile program (docs/spec/l3-program.md).

`nodes` is L3: programs and functions over tiles of tensors, in the ops of a
project's `ops.OpSet`; `text` its text, `verify` its checker, `interp` its numpy
reference. `legacy` is the old pass pipeline's graph IR, kept for its callers
until they are gone.
"""

from kohakuaccel.ir.l3.legacy import Buffer, Domain, GraphIR, Op

__all__ = ["Buffer", "Domain", "GraphIR", "Op"]
