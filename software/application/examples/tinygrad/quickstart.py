"""Plain tinygrad on the KohakuTPU simulator: no KohakuTPU import at all.

    python -m kohakutpu.application.ktpugrad --autoload   # once per venv
    DEV=KTPU python software/application/examples/tinygrad/quickstart.py

With the autoload hook installed, ``DEV=KTPU`` makes the simulated card
tinygrad's default device; every kernel tinygrad schedules runs there (a
generated DSL kernel -> L2 -> L1 on the Verilated card). Each result is
checked against numpy.
"""

import numpy as np
from tinygrad.device import Device
from tinygrad.tensor import Tensor

rng = np.random.default_rng(0)
x = rng.standard_normal((64, 128)).astype(np.float16)
w = (rng.standard_normal((96, 128)) / 11).astype(np.float16)
b = rng.standard_normal(96).astype(np.float16)


def F(a) -> np.ndarray:
    return np.asarray(a, np.float64)


print("default device:", Device.DEFAULT)

cases = {
    "gelu(x)": (
        lambda: Tensor(x).gelu(),
        lambda: 0.5
        * F(x)
        * (1 + np.tanh(np.sqrt(2 / np.pi) * (F(x) + 0.044715 * F(x) ** 3))),
    ),
    "softmax(x)": (
        lambda: Tensor(x).softmax(),
        lambda: np.exp(F(x) - F(x).max(1, keepdims=True))
        / np.exp(F(x) - F(x).max(1, keepdims=True)).sum(1, keepdims=True),
    ),
    "relu(x @ w.T + b)": (
        lambda: (Tensor(x) @ Tensor(w).T + Tensor(b)).relu(),
        lambda: np.maximum(F(x) @ F(w).T + b, 0),
    ),
    "x.T @ x": (lambda: Tensor(x).T @ Tensor(x), lambda: F(x).T @ F(x)),
    "cat + flip": (
        lambda: Tensor(x).cat(Tensor(x).flip(1), dim=1),
        lambda: np.concatenate([x, x[:, ::-1]], 1),
    ),
    "layernorm(x).sum(0)": (
        lambda: Tensor(x).layernorm().sum(0),
        lambda: (
            (F(x) - F(x).mean(1, keepdims=True))
            / np.sqrt(F(x).var(1, keepdims=True) + 1e-5)
        ).sum(0),
    ),
}
for name, (make, ref) in cases.items():
    got, want = F(make().numpy()), ref()
    print(f"{name:22s} rel err {np.abs(got - want).max() / np.abs(want).max():.2e}")
if Device.DEFAULT == "KTPU":
    rt = vars(Device["KTPU"])["rt"]  # the simulated card behind the device
    print(f"{len(rt.runs)} kernel runs, {sum(r.cycles for r in rt.runs):,} card cycles")
