"""Config-driven MNIST modules; registry entries contain architecture, not lowering rules."""

from functools import partial
from math import gcd, isqrt

from tinygrad import nn
from tinygrad.tensor import Tensor


def normalization(kind, channels, spatial=None, groups=4):
    """Construct a normalization in its producer's input shape."""
    if kind == "none":
        return None
    if kind == "group":
        return nn.GroupNorm(gcd(groups, channels), channels)
    if kind == "layer":
        return nn.LayerNorm((channels, spatial, spatial) if spatial else channels)
    if kind == "batch":
        return nn.BatchNorm(channels)
    raise ValueError(f"unknown normalization: {kind}")


class ConvNet:
    """Convolution stack and dense head, including a zero-convolution MLP.

    Residual blocks use learned 1x1 projections when channel count/stride changes.
    Pooling follows each block; normalization precedes activation.
    """

    def __init__(
        self,
        channels=(6, 16),
        kernels=(5, 5),
        strides=None,
        pool=None,
        padding=None,
        norm="group",
        groups=None,
        residual=False,
        activation="silu",
        head=(120, 84),
        head_norm=None,
        features=784,
    ):
        n = len(channels)
        if head_norm is None:
            head_norm = tuple("layer" if i == 0 else "none" for i in range(len(head)))
        strides = (1,) * n if strides is None else strides
        pool = (2,) * n if pool is None else pool
        padding = tuple(k // 2 for k in kernels) if padding is None else padding
        groups = (4,) * n if groups is None else groups
        if not (
            len(kernels)
            == len(strides)
            == len(pool)
            == len(padding)
            == len(groups)
            == n
        ):
            raise ValueError("one kernel/stride/pool/padding/group count per block")
        if activation not in ("relu", "silu", "gelu"):
            raise ValueError(f"unknown activation: {activation}")
        if len(head_norm) != len(head):
            raise ValueError("one head normalization per hidden width")
        self.activation, self.residual = activation, residual
        self.pool, self.blocks, self.hidden = tuple(pool), n, len(head)
        # A flat input of `features` values is one square channel for the conv stack.
        self.side = isqrt(features)
        size, cin = self.side, 1
        for i, (cout, kernel, stride, pad, group) in enumerate(
            zip(channels, kernels, strides, padding, groups), 1
        ):
            setattr(
                self, f"c{i}", nn.Conv2d(cin, cout, kernel, stride=stride, padding=pad)
            )
            size = (size + 2 * pad - kernel) // stride + 1
            setattr(self, f"g{i}", normalization(norm, cout, size, group))
            if residual:
                setattr(
                    self,
                    f"r{i}",
                    (
                        nn.Conv2d(cin, cout, 1, stride=stride)
                        if cin != cout or stride != 1
                        else None
                    ),
                )
            if pool[i - 1]:
                size = (size - pool[i - 1]) // pool[i - 1] + 1
            cin = cout
        width = cin * size * size if n else features
        for i, (out, kind) in enumerate(zip(head, head_norm), 1):
            setattr(self, f"l{i}", nn.Linear(width, out))
            setattr(self, f"n{i}", normalization(kind, out))
            width = out
        setattr(self, f"l{len(head) + 1}", nn.Linear(width, 10))

    def __call__(self, x: Tensor) -> Tensor:
        if self.blocks:
            x = x.reshape(-1, 1, self.side, self.side)
        for i in range(1, self.blocks + 1):
            skip = x
            x = getattr(self, f"c{i}")(x)
            norm = getattr(self, f"g{i}")
            if norm is not None:
                x = norm(x)
            if self.residual:
                projection = getattr(self, f"r{i}")
                x = x + (projection(skip) if projection is not None else skip)
            x = getattr(x, self.activation)()
            if self.pool[i - 1]:
                x = x.avg_pool2d((self.pool[i - 1],) * 2)
        x = x.flatten(1)
        for i in range(1, self.hidden + 1):
            x = getattr(self, f"l{i}")(x)
            norm = getattr(self, f"n{i}")
            if norm is not None:
                x = norm(x)
            x = getattr(x, self.activation)()
        return getattr(self, f"l{self.hidden + 1}")(x)


class LeNet(ConvNet):
    """Compatibility constructor with the original parameter names and draw order."""

    def __init__(self):
        super().__init__(padding=(2, 0), groups=(2, 4))


class MLP(ConvNet):
    def __init__(self, hidden=128, features=784):
        super().__init__(
            channels=(),
            kernels=(),
            head=(hidden,),
            head_norm=("none",),
            activation="relu",
            features=features,
        )


MODELS = {
    "lenet": LeNet,
    "lenet-bn": partial(ConvNet, padding=(2, 0), norm="batch"),
    "lenet-nonorm": partial(
        ConvNet, padding=(2, 0), norm="none", head_norm=("none", "none")
    ),
    "resnet-s": partial(
        ConvNet,
        channels=(8, 16, 16, 16),
        kernels=(3, 3, 3, 3),
        pool=(2, 2, 0, 0),
        residual=True,
        activation="relu",
        head=(64,),
        head_norm=("layer",),
    ),
    "convnet-w": partial(
        ConvNet,
        channels=(12, 24, 32),
        kernels=(5, 3, 3),
        pool=(2, 2, 0),
        norm="layer",
        activation="gelu",
        head=(96,),
        head_norm=("layer",),
    ),
    "mlp": MLP,
}
