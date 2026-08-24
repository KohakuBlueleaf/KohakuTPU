"""RUN THIS. SDXL's transformer block on the simulator, graded layer by layer.

Every number below is measured here, against a float32 host reference. Nothing
opens a card: `api.device()` resolves to the unit models unless asked otherwise.

Run from `demos/kohakutpu/`: `python -m sdxl.run`
"""

import ktpugrad
import numpy as np
from tinygrad.tensor import Tensor

from sdxl import kernels, nn
from sdxl import reference as R
from sdxl.model import BasicTransformerBlock

ktpugrad.install()
rng = np.random.default_rng(0)
RULE = "=" * 78


def head(title: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")


def rand(*shape, scale=0.35):
    return (rng.standard_normal(shape) * scale).astype(np.float16)


def dev(array):
    return Tensor(np.ascontiguousarray(array), device=ktpugrad.NAME)


head("The activations, one vector pass each")
print("SDXL uses SiLU in the resnets and GELU inside the GEGLU. Both are one")
print("kernel here; tinygrad schedules four ops for each.\n")

xa = rand(64, 128)
R.grade("silu", kernels.silu(dev(xa)), R.silu(xa.astype(np.float32)), R.ELEMENTWISE)
R.grade("gelu", kernels.gelu(dev(xa)), R.gelu(xa.astype(np.float32)), R.ELEMENTWISE)

head("GEGLU, both roads")
print("sdxl_original_unet.py:576 is one Linear of width 2N, then `.chunk(2)`:")
print("the FIRST half is the value, the second the gate. Two ways to run it.\n")

value, gate = rand(64, 256), rand(64, 256)
R.grade(
    "geglu_combine (elementwise)",
    kernels.geglu_combine(dev(value), dev(gate)),
    value.astype(np.float32) * R.gelu(gate.astype(np.float32)),
    R.ELEMENTWISE,
)

xb, vw, gw = rand(64, 128), rand(256, 128), rand(256, 128)
R.grade(
    "geglu_fused  (two GEMMs)",
    kernels.geglu_fused(dev(xb), dev(vw), dev(gw)),
    R.geglu_fused(*(a.astype(np.float32) for a in (xb, vw, gw))),
)

rt = ktpugrad.library._rt()
plan = kernels.geglu_fused_fn.plan(*(rt.tensor(a) for a in (xb, vw, gw)))
print(
    f"\n  fused: stages {len(plan.stages)} {[s.unit for s in plan.stages]}, "
    f"temps {len(plan.temps)}"
)
print("  Splitting the 2N Linear into two GEMMs means the 2N intermediate never")
print("  exists -- both tiles drain into one vector core. It is bias-free: an")
print("  epilogue takes ONE non-accumulator operand and a biased GEGLU wants two.")

head("LayerNorm, through the shipped kernel")
norm = nn.LayerNorm(128)
w, b = rand(128, scale=1.0), rand(128, scale=0.1)
norm.load_state_dict({"weight": w, "bias": b})
R.grade(
    "layernorm",
    norm(dev(xa)),
    R.layernorm(xa.astype(np.float32), w.astype(np.float32), b.astype(np.float32)),
    R.ELEMENTWISE,
)

head("Attention, through the flash kernel")
print("tinygrad schedules five kernels for scaled_dot_product_attention and the")
print("matcher declines all five, so this goes through the library directly.\n")

q, k, v = (rand(4, 64, 64) for _ in range(3))
R.grade(
    "attention",
    ktpugrad.attention(dev(q), dev(k), dev(v)),
    R.attention(*(a.astype(np.float32) for a in (q, k, v))),
)

head("A whole transformer block")
# SDXL's head is 64 wide at every level -- 320/5, 640/10, 1280/20 -- and 64 is
# two 32-channel blocks, which is why a head never splits one.
DIM, HEADS, HEAD_DIM, CTX, TOKENS = 256, 4, 64, 256, 64
block = BasicTransformerBlock(DIM, HEADS, HEAD_DIM, CTX)

state = {}
for name, param in block.named_parameters():
    scale = 1.0 if name.endswith("norm1.weight") else 0.25
    state[name] = rand(*param.shape, scale=scale) if param.shape else rand(1)
absent = block.load_state_dict(state)
print(
    f"  parameters {sum(1 for _ in block.named_parameters())}, "
    f"{block.nbytes() / 1024:.0f} KiB, missing {len(absent)}"
)

x = dev(rand(1, TOKENS, DIM))
context = dev(rand(1, CTX, DIM))
out = block(x, context)
print(f"  forward {tuple(x.shape)} x {tuple(context.shape)} -> {tuple(out.shape)}")
print("  Ran end to end on the unit models. The per-layer grades above are what")
print("  say it is right; a block-level number would hide which layer drifted.")

head("What the device could not do, and the host did instead")
print("Each of these is a REAL round trip through numpy, counted as it happened.")
print("They are the compiler's bill for this block, not an estimate.\n")
for why, count in sorted(nn.RELAYOUTS.items()):
    print(f"  {why:<28} {count:>4}")
print("\n  All three are the same refusal: the chain generator lowers a")
print("  BROADCAST, and a permute or a slice is a strided load. None of them")
print("  needs new silicon, and none of them needs to happen at all any more:")
print("  `kernels.project_heads` and `kernels.attn_out` make the head split an")
print("  N-partition of the projection, and `geglu` takes the two halves. This")
print("  demo has not been moved onto them; the count above is what it does.")

head("What this demo does NOT do yet")
print("  no real weights      -- Kohaku-XL-Zeta is ~6.5 GB; `weights.py` is next")
print("  no conv path         -- the resnets need conv2d's [C/32][H][W][32] layout")
print("  no VAE               -- TAESD decode is the last stage")
print("  narrow rows only     -- this demo folds nothing wider than VLMAX 128.")
print("                          `api.layernorm` handles 640 and 1280 now; the")
print("                          model here has not been widened to use it")
