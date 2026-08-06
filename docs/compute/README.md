# Compute

Everything inside a compute unit: the matmul datapath, the arithmetic primitives
it is built from, how those map onto DSP48E2, and what they cost.

Mirrors `src/kohakutpu/`.

| Doc | Covers |
|---|---|
| [matmul.md](matmul.md) | **Current design.** Tensor CU, cluster chain, accumulator network — logic level |
| [matmul-circuit.md](matmul-circuit.md) | The same design at primitive level — DSP48E2 packing, cascade, LUT budget |
| [arithmetic.md](arithmetic.md) | FP8 mul, FP16 FMA, FP24 FMA, division, log, exp — the bit-level constructions |
| [dsp.md](dsp.md) | DSP48E2 modes, pipelining, the `(A+D)*B+C` packing this project relies on |
| [costs.md](costs.md) | Measured resource cost and throughput per unit |
| [controller.md](controller.md) | CU instruction set and control state machines |

## Reading order

Start with [`matmul.md`](matmul.md) — it is the active design and states its own
assumptions. [`arithmetic.md`](arithmetic.md) and [`dsp.md`](dsp.md) are
reference material for the primitives it is composed from.

## Status

`matmul.md` describes the design being worked on now: microscaled integer
elements with an exact integer reduction inside a cluster, floating point only
above it.

The other four documents describe the **previous** FP8→FP12→FP16 design, which
is implemented in `src/kohakutpu/` and measured in `costs.md`. They are kept
because the arithmetic constructions and the DSP packing analysis carry over
directly, and because `costs.md` is the only measured baseline that exists.

Where the two disagree, `matmul.md` is current.

Two boundaries worth knowing:

- **Quantisation is not done here.** Elements arrive already quantised; MAS
  performs the conversion on the way from memory. See
  [`../noc/spec.md`](../noc/spec.md).
- **The NoC-facing side of a compute unit is not here either.** Instruction
  delivery, completion signalling and control registers are the CU framework:
  [`../noc/cu-framework.md`](../noc/cu-framework.md).
