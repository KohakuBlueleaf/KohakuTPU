"""A pretrained vision transformer moved onto the KohakuTPU simulator.

    KOHAKUTPU_BOARD=multimesh_v9 python software/application/examples/tinygrad/vit_mnist.py

The model is tinygrad modules (patch embedding, 2 encoder blocks of 2-head
attention + GELU MLP, a mean-pooled classifier) over 64 patches of 4 x 4.
It is trained in f32 with tinygrad on a host device (``--train-device``,
the CPU by default) and saved as safetensors; that checkpoint is loaded
into a fresh model, placed on the card in f16, and classifies test images
there. On the card the attention is the DSL `attention` kernel
(`ktpugrad.bridge`): one call a layer covers every (image, head) pair;
``--attention tinygrad`` runs tinygrad's own attention instead. Prints both
devices' accuracy, their agreement, the largest logit difference, cycles.
"""

import argparse
import pathlib
import time

import numpy as np
from tinygrad import nn
from tinygrad.helpers import Context
from tinygrad.nn.datasets import mnist
from tinygrad.nn.optim import Adam
from tinygrad.nn.state import (
    get_parameters,
    get_state_dict,
    load_state_dict,
    safe_load,
    safe_save,
)
from tinygrad.tensor import Tensor

from kohakutpu.application import ktpugrad
from kohakutpu.application.ktpugrad.nn import place
from kohakutpu.application.runtime import device as card
from kohakutpu.language.examples.dsl import attention

PATCH, TOKENS = 4, 64
CHECKPOINT = pathlib.Path.home() / ".cache" / "kohakutpu" / "vit_mnist.safetensors"

#: The DSL kernel as a function of Tensors: q, k [heads * T, hd], vt [heads * hd, T].
dsl_attention = ktpugrad.bridge(attention)


class Attention:
    def __init__(self, dim: int, heads: int, kernel: bool = True) -> None:
        self.heads, self.hd, self.kernel = heads, dim // heads, kernel
        self.qkv = nn.Linear(dim, 3 * dim)
        self.proj = nn.Linear(dim, dim)

    def __call__(self, x: Tensor) -> Tensor:
        B, T, D = x.shape
        H, hd = self.heads, self.hd

        qkv = self.qkv(x).reshape(B, T, 3, H, hd)
        q, k, v = (qkv[:, :, j].transpose(1, 2) for j in range(3))  # [B, H, T, hd]

        if self.kernel and x.device == ktpugrad.NAME:
            # Every (image, head) pair is one head of the kernel.
            o = dsl_attention(
                q.reshape(B * H * T, hd),
                k.reshape(B * H * T, hd),
                v.transpose(-2, -1).reshape(B * H * hd, T),
                heads=B * H,
            ).reshape(B, H, T, hd)
        else:
            o = (q @ k.transpose(-2, -1) * self.hd**-0.5).softmax(-1) @ v

        return self.proj(o.transpose(1, 2).reshape(B, T, D))


class FeedForward:
    def __init__(self, dim: int, hidden: int) -> None:
        self.fc1 = nn.Linear(dim, hidden)
        self.fc2 = nn.Linear(hidden, dim)

    def __call__(self, x: Tensor) -> Tensor:
        return self.fc2(self.fc1(x).gelu())


class Block:
    def __init__(self, dim: int, heads: int, hidden: int, kernel: bool) -> None:
        self.ln1, self.attn = nn.LayerNorm(dim), Attention(dim, heads, kernel)
        self.ln2, self.mlp = nn.LayerNorm(dim), FeedForward(dim, hidden)

    def __call__(self, x: Tensor) -> Tensor:
        x = x + self.attn(self.ln1(x))
        return x + self.mlp(self.ln2(x))


class ViT:
    def __init__(
        self,
        dim: int = 64,
        heads: int = 2,
        depth: int = 2,
        hidden: int = 128,
        classes: int = 10,
        kernel: bool = True,
    ) -> None:
        self.embed = nn.Linear(PATCH * PATCH, dim)
        self.pos = Tensor.randn(TOKENS, dim) * 0.02
        self.blocks = [Block(dim, heads, hidden, kernel) for _ in range(depth)]
        self.norm = nn.LayerNorm(dim)
        self.head = nn.Linear(dim, classes)

    def __call__(self, x: Tensor) -> Tensor:
        """[B, 64, 16] patches -> [B, 10] logits."""
        h = self.embed(x) + self.pos
        for block in self.blocks:
            h = block(h)
        return self.head(self.norm(h.mean(1)))


def patches(images: np.ndarray) -> np.ndarray:
    """[N, 1, 28, 28] uint8 -> [N, 64, 16] in [0, 1]: padded to 32 x 32, then
    4 x 4 patches row-major."""
    x = np.pad(images[:, 0] / 255.0, ((0, 0), (2, 2), (2, 2)))
    n, side = len(x), 32 // PATCH
    x = x.reshape(n, side, PATCH, side, PATCH).transpose(0, 1, 3, 2, 4)
    return x.reshape(n, TOKENS, PATCH * PATCH)


def pretrain(x, y, steps: int, batch: int, lr: float, device: str) -> dict:
    """The checkpoint: f32 Adam with tinygrad's own lowering on `device`."""
    rng = np.random.default_rng(0)
    t0 = time.monotonic()

    with Context(DEV=device):
        Tensor.manual_seed(0)
        model = ViT()
        params = get_parameters(model)
        for p in params:
            p.replace(p.cast("float32").realize())
        opt = Adam(params, lr=lr)

        with Context(TRAINING=1):
            for step in range(steps):
                idx = rng.integers(0, len(x), batch)
                xb = Tensor(x[idx].astype(np.float32))
                yb = Tensor(y[idx].astype(np.int32))

                loss = model(xb).sparse_categorical_crossentropy(yb)
                opt.zero_grad()
                loss.backward()
                Tensor.realize(loss, *opt.schedule_step())

                if step % 50 == 0 or step == steps - 1:
                    wall = time.monotonic() - t0
                    print(
                        f"  step {step:4d}  loss {loss.item():.4f}  ({wall:.0f} s)",
                        flush=True,
                    )

    return get_state_dict(model)


def logits(model, x: np.ndarray, device: str, dtype, batch: int = 32) -> np.ndarray:
    out = [
        model(Tensor(x[i : i + batch].astype(dtype), device=device)).numpy()
        for i in range(0, len(x), batch)
    ]
    return np.concatenate(out).astype(np.float64)


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--checkpoint", type=pathlib.Path, default=CHECKPOINT)
    ap.add_argument(
        "--train-device", default="CPU", help="tinygrad device to pretrain on"
    )
    ap.add_argument(
        "--steps", type=int, default=600, help="pretraining steps (no checkpoint yet)"
    )
    ap.add_argument("--batch", type=int, default=128)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--attention", choices=("dsl", "tinygrad"), default="dsl")
    ap.add_argument(
        "--test", type=int, default=16, help="test images classified on the card"
    )
    args = ap.parse_args()

    xtr, ytr, xte, yte = (t.numpy() for t in mnist(device="CPU"))
    xte, yte = patches(xte[: args.test]), yte[: args.test]

    # 1. The pretrained checkpoint: made once with tinygrad on a host device.
    if not args.checkpoint.exists():
        ktpugrad.uninstall()  # tinygrad's own lowering and f32 defaults
        print(f"pretraining on {args.train_device}: {args.steps} steps of {args.batch}")
        state = pretrain(
            patches(xtr), ytr, args.steps, args.batch, args.lr, args.train_device
        )
        args.checkpoint.parent.mkdir(parents=True, exist_ok=True)
        safe_save(state, str(args.checkpoint))
        ktpugrad.install()
    print(f"checkpoint: {args.checkpoint}")

    # 2. The host's answer: the checkpoint in f32 on the CPU.
    ktpugrad.uninstall()
    with Context(DEV="CPU"):
        host = ViT(kernel=False)
        load_state_dict(host, safe_load(str(args.checkpoint)))
        want = logits(host, xte, "CPU", np.float32)
    ktpugrad.install()

    # 3. The same checkpoint on the card in f16.
    with Context(DEV="CPU"):
        model = ViT(kernel=args.attention == "dsl")
    load_state_dict(model, safe_load(str(args.checkpoint)))
    place(model)

    rt = card.device()
    t0 = time.monotonic()
    got = logits(model, xte, ktpugrad.NAME, np.float16)
    wall = time.monotonic() - t0

    hit, ref = got.argmax(1), want.argmax(1)
    print(
        f"KTPU ({args.attention} attention), {len(xte)} images: accuracy {(hit == yte).mean():.4f} "
        f"(CPU f32 {(ref == yte).mean():.4f}), agree {(hit == ref).mean():.4f}, "
        f"max |logit diff| {np.abs(got - want).max():.3f}"
    )
    print(
        f"  {len(rt.runs)} kernel runs, {sum(r.cycles for r in rt.runs):,} card cycles, "
        f"{wall:.0f} s wall"
    )


if __name__ == "__main__":
    main()
