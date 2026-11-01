"""Architectures beside the MNIST family, each training on the same flat 28x28 input,
so the corpus holds token mixing, causal attention, skip connections, a recurrence
and wide dense layers."""

from functools import partial

import numpy as np
from examples.tinygrad.mnist_models import ConvNet
from tinygrad import nn
from tinygrad.tensor import Tensor


class Mixer:
    """MLP-Mixer: linear layers across tokens, then across channels."""

    def __init__(self, width=64, tokens=16, blocks=3):
        self.embed = nn.Linear(49, width)
        self.blocks = [
            (
                nn.LayerNorm(width),
                nn.Linear(tokens, tokens * 2),
                nn.Linear(tokens * 2, tokens),
                nn.LayerNorm(width),
                nn.Linear(width, width * 2),
                nn.Linear(width * 2, width),
            )
            for _ in range(blocks)
        ]
        self.head = nn.Linear(width, 10)

    def __call__(self, x: Tensor) -> Tensor:
        batch = x.shape[0]
        p = x.reshape(batch, 4, 7, 4, 7).permute(0, 1, 3, 2, 4).reshape(batch, 16, 49)
        x = self.embed(p)
        for norm1, up, down, norm2, fc1, fc2 in self.blocks:
            y = up(norm1(x).transpose(1, 2)).gelu()
            x = x + down(y).transpose(1, 2)
            x = x + fc2(fc1(norm2(x)).gelu())
        return self.head(x.mean(1))


class CausalBlock:
    def __init__(self, width, tokens):
        self.width, self.heads = width, width // 64
        self.norm1 = nn.LayerNorm(width)
        self.qkv = nn.Linear(width, 3 * width)
        self.proj = nn.Linear(width, width)
        self.norm2 = nn.LayerNorm(width)
        self.fc1 = nn.Linear(width, 2 * width)
        self.fc2 = nn.Linear(2 * width, width)
        above = np.triu(np.full((tokens, tokens), -32.0, np.float16), 1)
        self.mask = Tensor(above, device="CPU")
        self.mask.requires_grad = False  # type: ignore[assignment]

    def __call__(self, x: Tensor) -> Tensor:
        batch, tokens, _ = x.shape
        qkv = self.qkv(self.norm1(x)).reshape(batch, tokens, 3, self.heads, 64)
        q, k, v = (qkv[:, :, i].transpose(1, 2) for i in range(3))
        scores = (q @ k.transpose(-1, -2)) * 0.125 + self.mask.to(q.device)
        a = (scores.softmax(-1) @ v).transpose(1, 2).reshape(batch, tokens, self.width)
        x = x + self.proj(a)
        return x + self.fc2(self.fc1(self.norm2(x)).gelu())


class Gpt:
    """A decoder over the image rows as tokens, each row seeing only earlier rows."""

    def __init__(self, width=128, blocks=2):
        self.embed = nn.Linear(28, width)
        self.pos = Tensor.zeros(1, 28, width, device="CPU")
        self.blocks = [CausalBlock(width, 28) for _ in range(blocks)]
        self.norm = nn.LayerNorm(width)
        self.head = nn.Linear(width, 10)

    def __call__(self, x: Tensor) -> Tensor:
        x = self.embed(x.reshape(-1, 28, 28)) + self.pos.to(x.device)
        for block in self.blocks:
            x = block(x)
        return self.head(self.norm(x).mean(1))


class UNet:
    """Two scales down, nearest-neighbour up, skip by concatenation."""

    def __init__(self, width=8):
        self.down1 = nn.Conv2d(1, width, 3, padding=1)
        self.down2 = nn.Conv2d(width, width * 2, 3, padding=1)
        self.mid = nn.Conv2d(width * 2, width * 2, 3, padding=1)
        self.up = nn.Conv2d(width * 3, width, 3, padding=1)
        self.head = nn.Linear(width, 10)

    def __call__(self, x: Tensor) -> Tensor:
        x = x.reshape(-1, 1, 28, 28)
        a = self.down1(x).silu()
        pooled = a.max_pool2d((2, 2))
        b = self.down2(pooled).silu()  # type: ignore[arg-type]
        c = self.mid(b).silu()
        up = c.interpolate((28, 28), mode="nearest")
        return self.head(self.up(up.cat(a, dim=1)).silu().contiguous().mean(3).mean(2))


class Gru:
    """A gated recurrence over seven rows: many small launches a step."""

    def __init__(self, width=64, rows=7):
        self.rows, self.width = rows, width
        self.gate = nn.Linear(28 + width, width)
        self.update = nn.Linear(28 + width, width)
        self.fresh = nn.Linear(28 + width, width)
        self.head = nn.Linear(width, 10)

    def __call__(self, x: Tensor) -> Tensor:
        x = x.reshape(-1, 28, 28)
        h = Tensor.zeros(x.shape[0], self.width, device=x.device).contiguous()
        for t in range(self.rows):
            both = x[:, 4 * t].cat(h, dim=1)
            keep = self.gate(both).sigmoid()
            take = self.update(both).sigmoid()
            h = keep * h + take * self.fresh(both).tanh()
        return self.head(h)


MODELS = {
    "mixer": Mixer,
    "gpt": Gpt,
    "unet": UNet,
    "gru": Gru,
    "wide": partial(
        ConvNet,
        channels=(),
        kernels=(),
        head=(256, 256, 256),
        head_norm=("none", "none", "none"),
        activation="relu",
    ),
}
