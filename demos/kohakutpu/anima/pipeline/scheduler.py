"""Flow Matching ODE Scheduler for Anima DiT on KohakuTPU."""

import numpy as np
from tinygrad import Tensor


class FlowMatchScheduler:
    """Flow Matching ODE Euler Scheduler with sigma shifting."""

    def __init__(
        self,
        num_inference_steps: int = 30,
        shift: float = 3.0,
    ):
        self.num_inference_steps = num_inference_steps
        self.shift = shift
        self.timesteps: np.ndarray = np.array([])
        self.sigmas: np.ndarray = np.array([])
        self.set_timesteps(num_inference_steps, shift=shift)

    def set_timesteps(
        self,
        num_inference_steps: int = 30,
        denoising_strength: float = 1.0,
        shift: float | None = None,
    ):
        self.num_inference_steps = num_inference_steps
        shift = shift if shift is not None else self.shift

        t_raw = np.linspace(1.0, 0.0, num_inference_steps + 1, dtype=np.float32)
        sigmas = (shift * t_raw) / (1.0 + (shift - 1.0) * t_raw)

        if denoising_strength < 1.0:
            start_idx = int((1.0 - denoising_strength) * num_inference_steps)
            sigmas = sigmas[start_idx:]

        self.sigmas = sigmas
        self.timesteps = (sigmas * 1000.0).astype(np.float32)

    def step(
        self,
        model_output: Tensor,
        timestep_idx: int,
        sample: Tensor,
    ) -> Tensor:
        sigma_curr = self.sigmas[timestep_idx]
        sigma_next = self.sigmas[timestep_idx + 1]
        dt = float(sigma_next - sigma_curr)

        prev_sample = (sample + model_output * dt).realize()
        return prev_sample

    def add_noise(
        self,
        original_samples: Tensor,
        noise: Tensor,
        timestep_idx: int,
    ) -> Tensor:
        sigma = float(self.sigmas[timestep_idx])
        return (original_samples * (1.0 - sigma) + noise * sigma).realize()
