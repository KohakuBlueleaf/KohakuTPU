"""SDXL on KohakuTPU: PyTorch-shaped modules over tinygrad tensors.

Three rungs, and each layer picks the one that suits it: **tinygrad** for the
tensor algebra, **our DSL** for a `@kernel` that beats what tinygrad would
schedule (reached through :func:`ktpugrad.bridge`, and `kernels.py` holds them),
and **the library** where a shipped kernel is one dispatch against several.

`nn.py` is deliberately PyTorch's shape -- a `Module` owns parameters and
children, `load_state_dict` walks a checkpoint by name -- because that is what
makes an sd-scripts or diffusers reference readable beside this code.

**Simulation only.** Nothing here opens a card.
"""

from sdxl import kernels, nn
from sdxl.reference import grade

__all__ = ["grade", "kernels", "nn"]
