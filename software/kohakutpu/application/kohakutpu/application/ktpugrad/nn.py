"""Modules and training on an f16 card: `place` puts a module's tensors on a
device in a dtype, `Adam16` is AdamW whose every state is f16 on the card.

    with Context(DEV="CPU"):
        model = MLP()                  # tinygrad modules draw weights here
    place(model, "KTPU")
    opt = Adam16(nn.state.get_parameters(model), lr=1e-3)

    with Context(TRAINING=1):
        loss = loss_fn(model(x), y)
        opt.zero_grad()
        opt.scale(loss).backward()
        Tensor.realize(loss, *opt.schedule_step())  # loss read before the write
"""

from tinygrad.dtype import DType, dtypes
from tinygrad.helpers import TRAINING
from tinygrad.nn.optim import Optimizer
from tinygrad.nn.state import get_state_dict
from tinygrad.tensor import Tensor

from kohakutpu.application.ktpugrad.device import NAME


def place(module, device: str = NAME, dtype: DType = dtypes.half):
    """Every tensor of `module` (`get_state_dict`) moved to `device`, its
    floats cast to `dtype` where they are; the module, in place."""
    for t in get_state_dict(module).values():
        moved = t.cast(dtype) if dtypes.is_float(t.dtype) else t
        t.replace(moved.to(device).realize())
    return module


class Adam16(Optimizer):
    """AdamW on f16 weights with every state f16, run as card kernels; the
    update is computed at register precision and narrowed on writeback:
    w -= lr * (m * m_scale / (rms * r_scale + eps) + weight_decay * w).
    - the second moment is held as its root (`rms`), the gradient's own range;
    - the bias corrections m_scale, r_scale come from b1**t, b2**t kept on
      the card and advanced in the step's graph (a TinyJit replay is exact);
      `lr` is the base optimizer's tensor (a schedule assigns it, no rebuild);
    - `scale(loss)` multiplies the loss by `loss_scale` before `backward`, so
      small gradients stay above f16's subnormals; m / rms does not see it;
    - `compensate` keeps each weight's f16 rounding error (Kahan, against the
      decayed weight) and adds it to the next update.
    """

    def __init__(
        self,
        params: list[Tensor],
        lr: float = 1e-3,
        b1: float = 0.9,
        b2: float = 0.95,
        eps: float = 1e-8,
        weight_decay: float = 0.0,
        loss_scale: float = 1024.0,
        compensate: bool = True,
    ) -> None:
        super().__init__(params, lr)  # schedule_step is its own: never fused
        self.b1, self.b2, self.eps, self.wd = b1, b2, eps, weight_decay
        self.loss_scale, self.compensate = loss_scale, compensate
        self.steps = 0
        self.b1_t, self.b2_t = (
            Tensor([1.0], device=self.device).is_param_(False).realize()
            for _ in range(2)
        )

        self.m = [self._zeros(p) for p in self.params]
        self.rms = [self._zeros(p) for p in self.params]
        self.err = [self._zeros(p) for p in self.params] if compensate else []

    @staticmethod
    def _zeros(p: Tensor) -> Tensor:
        z = Tensor.zeros(*p.shape, dtype=p.dtype, device=p.device).contiguous()
        return z.is_param_(False).realize()

    def scale(self, loss: Tensor) -> Tensor:
        """The loss to call `backward` on: gradients come out `loss_scale` x."""
        return loss * self.loss_scale

    def schedule_step(self) -> list[Tensor]:
        if not TRAINING:
            raise RuntimeError("Adam16.step needs Context(TRAINING=1)")

        self.steps += 1
        # b1**t and b2**t live on the card and advance in the step's own
        # graph: a captured step (TinyJit) replays them, not frozen scalars.
        b1_t = self.b1_t.assign(self.b1_t * self.b1)
        b2_t = self.b2_t.assign(self.b2_t * self.b2)
        ms = 1 / (1 - b1_t)
        rs = (1 - b2_t).rsqrt()
        eps = self.eps * self.loss_scale  # rms is of the scaled gradient

        for i, p in enumerate(self.params):
            g = p.grad
            if g is None:
                raise RuntimeError(f"parameter {i} {p.shape} has no gradient")

            m = self.m[i].assign(self.b1 * self.m[i] + (1 - self.b1) * g)
            r = self.rms[i].assign(
                (self.b2 * self.rms[i] * self.rms[i] + (1 - self.b2) * g * g).sqrt()
            )

            up = m * ms / (r * rs + eps)
            if self.wd:
                up = up + p.detach() * self.wd
            step = self.lr * up

            if not self.compensate:
                p.assign((p.detach() - step).cast(p.dtype))
                continue

            # err is what the last write added in excess: y = -(step + err)
            # rounded, w + y rounded, then that write's excess kept exactly.
            y = (-(step + self.err[i])).cast(p.dtype).contiguous()
            new = (p.detach() + y).contiguous()
            self.err[i].assign((new - p.detach()) - y)
            p.assign(new)

        return [
            b1_t,
            b2_t,
            *self.m,
            *self.rms,
            *self.err,
            *self.params,
            *self.buffers,
        ]


__all__ = ["Adam16", "place"]
