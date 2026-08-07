# Compute

Everything inside a compute unit: the matmul datapath, the arithmetic primitives
it is built from, how those map onto DSP48E2, and what they cost.

Mirrors `src/kohakutpu/`.

| Doc | Covers |
|---|---|
| [tensor-isa.md](tensor-isa.md) | **Agreed design, not built.** Tensor descriptors, the L2/L1/tile hierarchy, convolution as a descriptor. What runs is [`../isa/cluster.md`](../isa/cluster.md) |
| [matmul.md](matmul.md) | **Current design.** Tensor CU, cluster chain, accumulator network — logic level |
| [matmul-circuit.md](matmul-circuit.md) | The same design at primitive level — DSP48E2 packing, cascade, LUT budget |
| [matmul-impl.md](matmul-impl.md) | **Built and verified.** One cluster: modules, benches, measured resources |
| [accumulator.md](accumulator.md) | Accumulator: precision and cost against mantissa width, and how it reached 300 MHz |
| [timing.md](timing.md) | **Pipeline stages, cycle counts, throughput and every measured resource number** |
| [arithmetic.md](arithmetic.md) | FP8 mul, FP16 FMA, FP24 FMA, division, log, exp — the bit-level constructions |
| [dsp.md](dsp.md) | DSP48E2 modes, pipelining, the `(A+D)*B+C` packing this project relies on |
| [costs.md](costs.md) | Measured resource cost and throughput per unit, for the superseded FP8→FP12 design |
| [controller.md](controller.md) | **Superseded.** The abandoned seven-type CU instruction set, and why it collapsed to three |

## Reading order

Start with [`matmul.md`](matmul.md) — it is the active design and states its own
assumptions. [`arithmetic.md`](arithmetic.md) and [`dsp.md`](dsp.md) are
reference material for the primitives it is composed from.

## Status

`matmul.md` describes the design being worked on now: microscaled integer
elements — **int7 significand with an E5M3 block scale shared by 32** — with an
exact integer reduction inside a cluster and floating point only above it.

`arithmetic.md`, `dsp.md`, `costs.md` and `controller.md` describe the
**previous** FP8→FP12→FP16 design. They are kept because the arithmetic
constructions and the DSP packing analysis carry over directly, and because
`costs.md` is the only measured baseline for the FP16 ALU path that exists. Any
mention of E4M3 or E5M2 in them belongs to that design and says nothing about
the current element format.

Where the two disagree, `matmul.md` is current.

Three boundaries worth knowing:

- **Quantisation is not done here, but it is built.** Elements arrive at a
  cluster already quantised; `src/kohakumas/mx_quant.v` converts FP16 to
  int7 + E5M3 on the way out of MAG. See [`../isa/memory.md`](../isa/memory.md)
  §6 for the format and §6.3 for why it sits there and not in the CU.
- **The NoC-facing side of a compute unit is not here either.** Instruction
  delivery, completion signalling and control registers are the CU framework:
  [`../noc/cu-framework.md`](../noc/cu-framework.md).
- **The instruction set a cluster runs is not here.** These documents are the
  datapath; [`../isa/cluster.md`](../isa/cluster.md) and
  [`../isa/acu.md`](../isa/acu.md) are what drives it.
