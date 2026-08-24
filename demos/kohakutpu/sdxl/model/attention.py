"""The transformer block SDXL repeats: self-attention, cross-attention, GEGLU.

Parameter names are diffusers' (`to_q`, `to_out.0`, `ff.net.0.proj`), so a
checkpoint loads by name. Structure follows sd-scripts.
"""

import ktpugrad
from tinygrad.tensor import Tensor

from sdxl.kernels import geglu_combine
from sdxl.nn import LayerNorm, Linear, Module, chunk2, relayout


class CrossAttention(Module):
    """Multi-head attention. `context` is None for self-attention.

    The score softmax goes through `ktpugrad.attention`, which is the shipped
    flash kernel -- one dispatch where tinygrad schedules five and matches none.
    """

    def __init__(self, dim: int, heads: int, dim_head: int, context: int | None = None):
        inner, ctx = heads * dim_head, context or dim
        self.heads, self.dim_head = heads, dim_head
        self.to_q = Linear(dim, inner, bias=False)
        self.to_k = Linear(ctx, inner, bias=False)
        self.to_v = Linear(ctx, inner, bias=False)
        # `:412` keeps this in a ModuleList, so the checkpoint says `to_out.0`.
        self.to_out = [Linear(inner, dim)]

    def _heads(self, x: Tensor) -> Tensor:
        """`[B, L, heads*dim_head]` -> `[B*heads, L, dim_head]`, through the host.

        The device cannot do this: the split is a strided permute and the chain
        generator lowers broadcasts only. `relayout` counts what it costs.
        """
        b, length, _ = x.shape
        # REALISED first: a reshape left lazy is fused back into the projection
        # and the matmul becomes a rank-4 reduce the matcher declines.
        flat = x.contiguous().realize()
        split = flat.reshape(b, length, self.heads, self.dim_head)
        moved = relayout("attention head split", split, (0, 2, 1, 3))
        return moved.reshape(b * self.heads, length, self.dim_head)

    def forward(self, x: Tensor, context: Tensor | None = None) -> Tensor:
        source = x if context is None else context
        q, k, v = self.to_q(x), self.to_k(source), self.to_v(source)
        out = ktpugrad.attention(self._heads(q), self._heads(k), self._heads(v))
        b, length = x.shape[0], x.shape[1]
        split = out.reshape(b, self.heads, length, self.dim_head)
        back = relayout("attention head join", split, (0, 2, 1, 3))
        joined = back.reshape(b, length, self.heads * self.dim_head)
        return self.to_out[0](joined)


class GEGLU(Module):
    """`sdxl_original_unet.py:557`. One Linear of width `2*out`, then chunked.

    The first half is the value and the second is the gate -- `:577` reads
    `hidden_states, gate = proj(x).chunk(2, dim=-1)`. `geglu_combine` is the
    elementwise half as one vector pass.
    """

    def __init__(self, dim_in: int, dim_out: int) -> None:
        self.proj = Linear(dim_in, dim_out * 2)
        self.dim_out = dim_out

    def forward(self, x: Tensor) -> Tensor:
        both = self.proj(x).reshape(-1, self.dim_out * 2)
        # Chunked on the HOST: a slice of the 2N projection is a strided view,
        # and the device cannot materialise one -- the copy itself refuses.
        value, gate = chunk2("geglu chunk", both, self.dim_out)
        return geglu_combine(value, gate).reshape(*x.shape[:-1], self.dim_out)


class FeedForward(Module):
    """`net.0` is the GEGLU, `net.1` an Identity, `net.2` the projection out.

    The list is kept as `net` so the checkpoint's `net.0.proj.weight` and
    `net.2.weight` load by name.
    """

    def __init__(self, dim: int) -> None:
        inner = dim * 4
        self.net = [GEGLU(dim, inner), None, Linear(inner, dim)]

    def forward(self, x: Tensor) -> Tensor:
        return self.net[2](self.net[0](x))


class BasicTransformerBlock(Module):
    """Norm, self-attend, norm, cross-attend, norm, feed forward. All residual."""

    def __init__(self, dim: int, heads: int, dim_head: int, context: int) -> None:
        self.norm1, self.norm2, self.norm3 = (LayerNorm(dim) for _ in range(3))
        self.attn1 = CrossAttention(dim, heads, dim_head)
        self.attn2 = CrossAttention(dim, heads, dim_head, context=context)
        self.ff = FeedForward(dim)

    def forward(self, x: Tensor, context: Tensor) -> Tensor:
        # REALISED at every residual: left lazy the three chains fuse into one
        # kernel needing five operands, and a band takes three.
        x = (x + self.attn1(self.norm1(x))).contiguous().realize()
        x = (x + self.attn2(self.norm2(x), context)).contiguous().realize()
        return (x + self.ff(self.norm3(x))).contiguous().realize()
