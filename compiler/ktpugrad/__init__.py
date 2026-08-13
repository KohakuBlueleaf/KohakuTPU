"""tinygrad as an OPTIONAL frontend onto KohakuTPU's kernel library.

Not a replacement for level 5. Its scheduler fuses an elementwise chain into the
consumer kernel, which on this machine is a matmul with a vector-core epilogue
-- exactly `linear_silu`. It does NOT keep our multi-stage kernels together:
`softmax` is four tinygrad kernels against one of ours, and the norms and
attention lose the same way, so the library stays reachable directly.

    import ktpugrad
    ktpugrad.install()
    (Tensor(x, device="KTPU") @ Tensor(w, device="KTPU")).numpy()

`softmax`, `rmsnorm`, `layernorm` and `attention` are the ESCAPE HATCH for the
kernels the scheduler splits -- called by name, never reached by a fallback.

An expression the library has no kernel for is GENERATED as one vector chain
rather than refused, including a reduction along the last axis. A matched kernel
always wins; the generator takes what is left, and refuses by name what it
cannot spell.

The hatch is still the FASTER answer for the row-wise kernels: the scheduler
gives a reduction its own kernel whatever we do, so a generated `softmax` runs
as three dispatches against the hatch's one stage.

Design in `.plan/TINYGRAD.md`; the seam is :mod:`ktpugrad.runner`, the hatch is
:mod:`ktpugrad.library` and the generator is :mod:`ktpugrad.chain`.
"""

from ktpugrad.chain import Load, Op, Recipe, Scalar, Source, read_chain, refusal
from ktpugrad.device import (
    NAME,
    KTPUAllocator,
    KTPUDevice,
    KTPURenderer,
    register,
)
from ktpugrad.errors import ChainRefused, KTPUUnsupported
from ktpugrad.generated import FOLDS, KERNELS, build
from ktpugrad.library import attention, layernorm, rmsnorm, softmax
from ktpugrad.match import Addend, Copy, Match, Operand, match_ast, summarise
from ktpugrad.runner import (
    ChainRunner,
    CopyRunner,
    KTPUProgram,
    LibraryRunner,
    capture,
    get_runner,
    install,
    to_program,
    uninstall,
)
from ktpugrad.tiling import plan

__all__ = [
    "FOLDS",
    "KERNELS",
    "NAME",
    "Addend",
    "ChainRefused",
    "ChainRunner",
    "Copy",
    "CopyRunner",
    "KTPUAllocator",
    "KTPUDevice",
    "KTPUProgram",
    "KTPURenderer",
    "KTPUUnsupported",
    "LibraryRunner",
    "Load",
    "Match",
    "Op",
    "Operand",
    "Recipe",
    "Scalar",
    "Source",
    "attention",
    "build",
    "capture",
    "get_runner",
    "install",
    "layernorm",
    "match_ast",
    "plan",
    "read_chain",
    "refusal",
    "register",
    "rmsnorm",
    "softmax",
    "summarise",
    "to_program",
    "uninstall",
]
