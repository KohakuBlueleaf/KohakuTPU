# Architecture

KohakuTPU: compute clusters on a NoC mesh, reaching DRAM through AXI4.
Target `xcvu13p-fhgb2104-2L-e` at **300 MHz**.

This is the entry point — the machine top to bottom, and how a matmul actually
flows through it. Every subsystem has its own document; this one exists so the
whole shape is in one place.

```
   {JTAG, PCIe}  <->  AXI4  <->  { DDR4, MAS, NoC mesh <-> clusters }
```

---

## 1. The machine

```
    host
      | AXI4
   +--------------+
   | orchestrator |   stages programs, dispatches instructions, mirrors status
   +--------------+   no AXI master: it never touches operand data
      |
   ===== NoC mesh (288-bit flits, XY routing, credit-based) =====
      |                    |                        |
   [cluster 0]        [cluster 1]   ...          [ MAS ]  <-> DRAM
    2 ports            2 ports                    memory access
```

**The orchestrator has no AXI master.** A cluster fetches its own operands by
issuing `MEM_RD_REQ` naming *itself* as source, so the reply is delivered
straight back and never passes through the orchestrator. That is what keeps the
orchestrator a control plane rather than a data bottleneck
([`noc/spec.md`](noc/spec.md) §10.3).

## 2. A cluster, and why it takes two NoC ports

```
   NoC <-> cluster manager <-> tcu -> tcu -> tcu -> tcu -> acu <-> NoC
   port 0   L2 / L1              direct DSP cascade         port 1
            descriptors          (PCOUT/PCIN + W port)      resident tile
            program              256 DSP, 0 LUT             5 BRAM36
```

The four tensor CUs are **not** NoC nodes. They are wired to each other by the
DSP48E2 cascade, which is what makes them cost zero LUTs — the multiply *and*
the whole K=32 reduction happen inside the DSPs. Only the manager and the
accumulator face the network.

Two ports, not five, and the reason is arithmetic. The chain consumes
`A[4][32] + B[32][4]` every cycle — **eight 256-bit operand words** — and one
NoC port delivers **one**. Feeding the TCUs directly is an 8× deficit however
many ports you spend. Reuse closes it instead: a cluster computing a `Gm × Gn`
block of output sub-tiles from local operands needs

```
   words/cycle  =  4 (Gm + Gn) / (Gm · Gn)      =  0.375  at 16 x 32
```

so one port carries operands and one carries results. **64 NoC ports for 32
clusters, not 160.**

## 3. Numbers, in one place

```
   MXFP7 element     E8M0 scale shared by 32 + 7-bit signed significand
   accumulator       FP22  S1E7M14
   result            FP16
   cluster           4 x (4x8x4 TCU) + 1 accumulator = 512 MACs/cycle
   quantisation      K = 32, which is also the cluster's K span
```

| | per cluster | ×32 | ×48 |
|---|---|---|---|
| LUT | 13,921 | 445,472 | 668,208 (38.7%) |
| BRAM36 | 5 | 160 | 240 (8.9%) |
| DSP | 256 | 8,192 | 12,288 (**100%**) |
| NoC ports | 2 | 64 | 96 |
| MACs/cycle | 512 | 16,384 | 24,576 |

At 300 MHz, 48 clusters is **~14.7 TFLOPS of AMP FP16-MXFP7**, DSP-bound.
FLOPS rather than IOPS: MXFP7 is a floating-point format (a shared power-of-two
exponent and a significand), operands and results in memory are FP16, and the
integer datapath inside the DSP is how an MXFP7 multiply is implemented once the
shared exponent is factored out. See [`compute/matmul.md`](compute/matmul.md)
§3.0.

## 4. Workflow: how a matmul actually runs

```
   1  host writes a program into the orchestrator's staging buffer over AXI
             |
   2  orchestrator dispatches CU_INST flits to a cluster manager, credit-limited
             |
   3  FILL    manager walks a tensor descriptor, issues its own MEM_RD_REQ,
             |  assembles replies into 928-bit L1 entries
   4  GEMM    manager sweeps K OUTERMOST over the output sub-tiles, feeding the
             |  cascade one A entry and one B entry per cycle
             |  -> accumulator LOAD on the first K block, ADD after
   5  DRAIN   accumulator converts each sub-tile to FP16 and writes it back
             |  through its OWN port
   6  each instruction raises a completion signal; the orchestrator mirrors it
             |  into NODE_STATUS, the host polls over AXI
```

Two properties of step 4 are load-bearing rather than incidental:

**K is the outer loop.** Sweeping sub-tiles inside K means a given accumulator
address recurs only every `Gm·Gn` cycles instead of every cycle. That removes
the read-after-write recurrence entirely, which is what lets the resident tile
be a plain block RAM with a registered read — and deleted the three rotating
banks, the `EMIT` fold and the zero mask that the old K-inner order required.

**L1 is explicitly managed, never a cache.** No tags, no misses, no eviction.
The manager owns it and fills it by instruction, because only it knows the loop
structure. That is also what makes the memory instructions expressive enough to
cover convolution.

## 5. Convolution is a memory request

The compute instruction for a convolution is **byte-identical** to the one for a
matmul. Only the descriptor changes.

A tensor descriptor is an N-dimensional affine address generator with bound
axes. For `conv2d`, the activation at im2col row `(n, oy, ox)` and column
`(ky, kx, c)` is `input[n][oy·S+ky-P][ox·S+kx-P][c]` — affine in six loop
indices, with two bounded axes for padding:

```
   dim     n     oy     ox     ky    kx    c
   stride  sN    S·sH   S·sW   sH    sW    sC
   axis    -     H      W      H     W     -
```

Out-of-range addresses inject zeros and issue no memory request, so padding
needs no handling anywhere else in the machine. No im2col buffer is
materialised. See [`compute/tensor-isa.md`](compute/tensor-isa.md) §3.2.

## 6. Where to read next

| you want | read |
|---|---|
| the cluster ISA, descriptors, memory hierarchy | [`compute/tensor-isa.md`](compute/tensor-isa.md) |
| the matmul datapath and formats | [`compute/matmul.md`](compute/matmul.md) |
| DSP48E2 packing and the cascade | [`compute/matmul-circuit.md`](compute/matmul-circuit.md) |
| what is built, measured resources | [`compute/matmul-impl.md`](compute/matmul-impl.md) |
| accumulator precision and the road to 300 MHz | [`compute/accumulator.md`](compute/accumulator.md) |
| the machine running end to end | [`system.md`](system.md) |
| packet format, routing, CU interface | [`noc/spec.md`](noc/spec.md) |
| how to run and write benches | [`simulation.md`](simulation.md) |

## 7. Status

```
   matmul datapath        built, exact against both DSP models
   FP22 accumulator       349.4 MHz, 5 BRAM36
   2-port cluster         322.4 MHz, 13,921 LUT, 256 DSP -- meets 300
   tensor descriptors     built, conv2d im2col validated
   2x2 mesh, 2 clusters   C[32,64] = A[32,128]xB[128,64] end to end
   orchestrator, NoC      built, mesh and multi-CU benches pass
   MAS, AXI bridge        not started
   quantiser              not started -- operands are preloaded as MXFP7
   FILL via descriptors   walker built, not yet wired into the fill engine
```

> The vector and general-purpose units in the original sketch are not built.
> The FP8→FP12→FP16 tensor core they were designed around has been superseded by
> the MXFP7 design; [`compute/arithmetic.md`](compute/arithmetic.md) and
> [`compute/costs.md`](compute/costs.md) retain its measured numbers, which are
> still the only baseline for the FP16 ALU path.
