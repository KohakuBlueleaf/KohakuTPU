"""Explicitly selected, timed Verilated pilots; never run in the offline suite."""

import os
import runpy
import sys
import time
from pathlib import Path

import numpy as np
import pytest
from tinygrad import Tensor

from kohakutpu.application import api as kp
from kohakutpu.application import ktpugrad
from kohakutpu.application.api.attention import attention
from kohakutpu.application.ktpugrad.attention import attention as tg_attention
from kohakutpu.application.runtime import device as D


@pytest.mark.skipif(
    not os.environ.get("VIT_MODEL_PROBE"), reason="explicit model pilot only"
)
def test_model_pilot(monkeypatch):
    case = os.environ["VIT_MODEL_PROBE"]
    tokens = int(os.environ.get("VIT_MODEL_TOKENS", "1"))
    start = time.monotonic()
    rng = np.random.default_rng(int(os.environ.get("VIT_MODEL_SEED", "19")))
    if case == "block":
        script = Path(__file__).resolve().parents[2] / "examples/tinygrad/vit_tiny.py"
        ns = runpy.run_path(str(script))
        monkeypatch.setattr(
            sys,
            "argv",
            [
                str(script),
                "--blocks",
                "1",
                "--width",
                "64",
                "--image-size",
                str(tokens),
            ],
        )
        ns["main"]()
    else:
        with D.attached(D.Device("sim", board="vlt_v9t6s_1n")):
            if case == "softmax":
                x = rng.standard_normal((1, tokens, 197)).astype(np.float16)
                z = x.astype(np.float32)
                e = np.exp(z - z.max(-1, keepdims=True))
                want = e / e.sum(-1, keepdims=True)
                got = kp.tensor(x).softmax().numpy().astype(np.float32)
            else:
                queries = int(os.environ.get("VIT_MODEL_QUERIES", str(tokens)))
                q, k, vt = [
                    rng.standard_normal(s).astype(np.float16)
                    for s in ((1, queries, 64), (1, tokens, 64), (1, 64, tokens))
                ]
                scores = (
                    q.astype(np.float32) @ k.astype(np.float32).swapaxes(-1, -2)
                ) * np.float32(0.125)
                e = np.exp(scores - scores.max(-1, keepdims=True))
                want = (e / e.sum(-1, keepdims=True)) @ vt.astype(np.float32).swapaxes(
                    -1, -2
                )
                if case in ("tg", "tg_split"):
                    ktpugrad.install()
                    tq, tk, tv = [
                        Tensor(a, device="KTPU")
                        for a in (q, k, vt.swapaxes(-1, -2).copy())
                    ]
                    result = (
                        tg_attention(tq, tk, tv)
                        if case == "tg_split"
                        else (((tq @ tk.transpose(-1, -2)) * 0.125).softmax(-1) @ tv)
                    )
                    got = result.numpy().astype(np.float32)
                else:
                    got = (
                        attention(
                            kp.tensor(q),
                            kp.tensor(k),
                            kp.tensor(vt),
                            precise_scores=case == "precise",
                        )
                        .numpy()
                        .astype(np.float32)
                    )
            error = float(
                np.abs(got - want).max() / max(float(np.abs(want).max()), 1e-9)
            )
            print("VIT_MODEL", case, "tokens", tokens, "error", error, flush=True)
            assert np.isfinite(got).all() and error < (
                0.02 if case == "softmax" else 0.03
            )
    print("VIT_MODEL_SECONDS", time.monotonic() - start, flush=True)
