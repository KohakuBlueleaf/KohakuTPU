# Vector bring-up: 4 cores, 4 MAG ports, no matmul

**Design.** The standalone configuration that gets a vector core running end to
end — mesh, driver, kernel codegen, cost model, live simulator — with **no
matmul unit in the machine at all.**

That exclusion is the point rather than a simplification. Every existing bench
reaches the vector path through a GEMM, so a vector defect would arrive dressed
as a matmul defect, several modules from where it happened. A machine with only
vector cores makes a vector failure attributable — the same argument that put
`MODEL=1` beside `MODEL=0` in every arithmetic bench.

The ALU is built and measured ([vector-core.md](vector-core.md) §13). The core
around it, and everything here, is not.

---

## 1. The mesh

MAG's four ports, four vector cores, one row each.

```
            col 0            col 1            col 2
   row 0    MAG port 0       VC0 port A       VC0 port B
   row 1    MAG port 1       VC1 port A       VC1 port B
   row 2    MAG port 2       VC2 port A       VC2 port B
   row 3    MAG port 3       VC3 port A       VC3 port B
```

Twelve routers. **Nothing interleaves**: routing is X-then-Y on clamped
coordinates, so a core's traffic goes `(2,y) → (1,y) → (0,y)` and never leaves
row `y`. No core's memory traffic crosses another's, which is the layout rule
the cluster mesh already follows and the reason it was chosen there.

Scales by rows: `NVC = 1, 2, 4` uses that many rows and that many MAG ports.
Above 4 the ports are shared, exactly as clusters share them today.

---

## 2. The number that decides everything: one MAG port per core

A NoC flit payload is 256 bits (`noc_pkt.vh`), full duplex. A vector core has
two ports, **but both funnel to one MAG port**, so the port is the ceiling and
the port count is not:

```
   read into a core     256 bit/cycle  =  16 FP16 elements/cycle
   write out of a core  256 bit/cycle
```

A flat elementwise `C = A op B` costs two input elements per result:

```
   results/cycle  =  16 / 2  =  8         against 16 ALUs
```

**Flat mode runs the ALUs at 50% in this configuration.** And then:

| mode | ops per result | ops/cycle at 8 results/cycle | ALU utilisation |
|---|---|---|---|
| `FLAT` | 1 | 8 | 50% |
| **`D2`** | 2 | **16** | **100% — exactly saturated** |
| `D4` | 4 | 32 | 100%, 2× headroom |

`D2` saturates *exactly*. That is a coincidence of this configuration rather
than a law, but it is the right coincidence to build the bring-up around,
because it means the first kernel that fuses two operations is also the first
kernel that uses the machine fully.

**Peak: 4 × 16 × 2 flop × 300 MHz = 38.4 GFLOP/s**, reached only at D2 or
deeper.

### 2.1 Which kernels are compute-bound, and it is not the obvious ones

Per element: `compute = ops/16` cycles, `bandwidth = bytes/32` cycles, and the
kernel takes the larger.

| kernel | ops | FP16 bytes | compute | bandwidth | bound by | ALU use |
|---|---|---|---|---|---|---|
| `C = A + B` | 1 | 6 | 0.063 | 0.188 | **memory** | 33% |
| `D = A*B + C` | 1 | 8 | 0.063 | 0.250 | **memory** | 25% |
| `relu(A)` | 1 | 4 | 0.063 | 0.125 | **memory** | 50% |
| `exp2(A)` | 1 | 4 | 0.063 | 0.125 | **memory** | 50% |
| **`sigmoid(A)`** | 4 | 4 | 0.250 | 0.125 | **compute** | **100%** |
| **`silu(A)`** | 5 | 4 | 0.313 | 0.125 | **compute** | **100%** |
| **`gelu(A)`** | ~9 | 4 | 0.563 | 0.125 | **compute** | **100%** |
| `rmsnorm` (fused) | ~4 | 4 | 0.250 | 0.125 | **compute** | **100%** |

**Every unfused elementwise kernel is memory-bound and every fused one is
compute-bound**, and the crossover is at 2 ops per element — the same place
`D2` sits. So the codegen's single most important job is not instruction
selection, it is **fusion**: a kernel that reads a tensor to do one thing to it
has already lost half the machine before it issues an instruction.

This is also the argument for putting vector cores *near* the matmul rather
than treating them as a separate pass over DRAM. An epilogue that consumes a
matmul result while it is still on the mesh pays no read bandwidth at all.

---

## 3. Driver: what has to exist

Mirroring the matmul side, which is the reason the split is what it is.

```
   vec_isa.py       the 32-bit instruction word: encode, decode, disassemble
   vec_kernel.py    grid, tiling and codegen -- kernels out, flits in
   vec_model.py     TWO models: what it computes, and how long it takes
   vec_bench.py     shapes, memory layout, presets      (mirrors bench.py)
```

Nothing above the vector core changes. The kernel is staged and kicked exactly
as a GEMM pass is: the control program, the dispatch registers and the agent are
untouched ([isa/vector.md](../isa/vector.md) §8).

**A kernel is cheap to stage.** An instruction is 32 bits and a flit payload is
256, so **8 instructions per flit** — a 128-instruction kernel is 16 flits,
against 5 to 97 for a single GEMM pass. Staging is not a limiter here, which is
a pleasant inversion of the matmul side where `STAGE_FLITS` is what admits a
pass to a round.

### 3.1 The grid

Triton's shape, because the analogy is exact — a vector core running one kernel
instance is one program in a 1D grid:

```
   grid = ceil(elements / TILE_E)
   instance i  processes  [i*TILE_E, (i+1)*TILE_E)
```

`TILE_E = 1024` elements to start: 8 chunks of `VL = 128`, which is enough to
double-buffer L1 and amortise the kick, and small enough that four cores get
even work on tensors from about 4k elements up. Instances round-robin over
cores, and **the grid is over elements, not over cores** — that is the mistake
the matmul side made (`.plan/measurements/dispatch-n-only.md`), and it is worth
not repeating in a fresh file.

### 3.2 The kernels to build first, in order

1. `ew1(f)` — `C = f(A)`, `f` in {copy, neg, abs, relu, exp2, log2, inv, rsqrt}
2. `ew2(op)` — `C = A op B`, op in {add, sub, mul, max, min}
3. `fma3` — `D = A*B + C`
4. `act(f)` — `sigmoid, tanh, silu, gelu` — **the first compute-bound kernels**,
   and the first that need `D4`
5. `reduce(kind)` — `sum, max, sumsq` — the first that need `TREE` and the
   16-deep rotating accumulator (§4.2)
6. `rmsnorm` — reduce then scale, two passes fused into one residency
7. `softmax` — max-reduce, subtract-and-exp2, sum-reduce, scale

1 to 3 are memory-bound and exist to prove the load/store path. **4 is the first
kernel that is actually about the ALU**, and 5 is the first that is about the
core rather than the lane.

---

## 4. The two models

`vec_model.py` holds both, and keeping them in one file is deliberate: a cost
model that disagrees with the numeric model about what the kernel *did* is
worse than either alone.

### 4.1 The numeric model — bit-accurate E8M15 in Python

There is no RTL for the core yet, so kernels must be validated against
something. `scripts/py/vec_tables.py` already contains a **bit-exact integer
model of both Horner stages**, and it is what generated the coefficient ROMs. It
extends naturally into a full E8M15 emulator: pack/unpack, the FMA with the same
alignment and round-to-nearest-even, and the four seeds through the existing
`eval_fixed`.

That is worth having for its own sake. It means a kernel can be proved correct
**before any core RTL exists**, and when the RTL does exist the emulator is the
golden model the bench checks against — the same relationship `mxfp7.py` has to
the quantiser today.

Reuse, do not re-derive: if the emulator's FMA and `vec_alu.v` ever disagree
about a rounding, the one that is wrong is whichever was written second, and
having them in different languages is not an excuse for having them differ.

### 4.2 The cost model

Per grid instance, with double buffering so fill overlaps compute:

```
   compute   = ceil(ops_per_elem * TILE_E / 16)        cycles, II = 1
   memory    = ceil(bytes_per_elem * TILE_E / 32)      cycles, 256 bit/cycle
   steady    = max(compute, memory)
   latency   = 14 * chain_depth  +  L1 fill  +  NoC round trip
   total     = steady + latency + kick
```

Two things the model must get right or it will flatter the design:

- **The 14-cycle ALU latency is not free in a reduction.** A `TREE` pass is four
  levels, so 56 cycles before the first result, and the accumulator needs 16
  rotating partials to run at II=1 at all ([vector-core.md](vector-core.md)
  §7.3). A model that charges `elements/16` for a `sum` and stops is wrong by
  the tail.
- **`max(compute, memory)` assumes the double buffer works.** It only does if
  `TILE_E` is large enough to cover the fill latency. Below that the model
  should charge the sum, not the max — and say which regime it is in, because
  "why is this kernel slower than the roofline" is the question the simulator
  exists to answer.

The matmul cost model (`sim.py`, `MEASURED` / `*_BASIS`) is the pattern:
measured coefficients against a stated basis, so a number that drifts is
visible. **Until there is RTL, every coefficient here is derived and must be
labelled as such** — the vector core has no measured constants and pretending
otherwise would put unearned numbers in the visualiser.

---

## 5. The live simulator and what it should show

The matmul visualiser answers "where did the cycles go". The vector one has a
different question, because the answer is nearly always the same: **the roofline
is the whole story**, so it should be the whole page.

- **The roofline itself** — arithmetic intensity on x, achieved rate on y, the
  memory ceiling as a slope and the 38.4 GFLOP/s compute ceiling as a line, with
  the current kernel plotted on it. Then the mode markers: where `FLAT`, `D2`
  and `D4` put the same kernel. A user should be able to *see* that fusing two
  ops moves the point off the slope and onto the ceiling.
- **The 3×4 mesh**, MAG ports and cores, with each core's row lit by traffic —
  reusing the existing mesh widget, since the layout is deliberately the same
  shape.
- **Per-core timeline** — fill / compute / drain per grid instance, so a
  double-buffer that is not covering shows up as a gap rather than as a number.
- **The kernel listing**, disassembled from the actual emitted flits. The
  matmul viz already does this for CU instructions and it is the feature that
  makes the page a design tool rather than a dashboard.
- **The numeric result**, against the Python emulator: max and p50 relative
  error, in ulps. Not a threshold — a measurement, per the verdict rule that a
  FAIL means no output, a hang or an error.

### 5.1 What it must not do

Not report a rate as measured. There is no RTL, so every figure is the cost
model's opinion; the page should say so once, prominently, and label the axis
`modelled`. The matmul page earns the word "measured" because an xsim run
produced its numbers; this one will not until the core exists.

---

## 6. Build order

Each step is testable without the one after it, and none needs RTL until the
last.

1. `vec_isa.py` + the disassembler. Testable alone: encode/decode round-trips.
2. The **E8M15 emulator** in `vec_model.py`, checked against
   `scripts/py/vec_tables.py`'s existing bit-exact table model and against the
   `vec_alu_tb` figures already recorded (FMA correctly rounded, seeds ~0.55 ulp).
3. `vec_kernel.py` for kernels 1–3, with the emulator as the reference.
4. The cost model and the roofline page. **This is the first point where the
   design can be argued about with numbers rather than adjectives**, and it is
   deliberately before any RTL.
5. Kernels 4–7, which need `D4` and `TREE` and will exercise the parts of the
   cost model most likely to be wrong.
6. Then the core RTL, with the emulator as the golden model and the cost model
   as the thing to falsify.

Step 4 before step 6 is the whole point of doing it this way: the cheapest time
to discover that `TILE_E` is too small, or that the tree tail dominates a short
reduction, is before the RTL exists.

---

## 7. Open

- **`TILE_E = 1024` is a guess.** It should fall out of the fill latency once
  that is modelled, not the other way round.
- **`VL = 128` against 16 lanes** gives 8 elements per lane per register. Real
  kernels may want longer vectors and fewer registers, or the reverse; the
  histogram that would settle it is the same one §14 wants for chain depth.
- **Whether the vector core should read a matmul result off the mesh directly**
  rather than through DRAM. §2.1 says the bandwidth case is strong; the
  scheduling case is not made, and it interacts with split-K
  ([optimization.md](../optimization.md) §J3).
- **Four cores against four ports is a chosen simplification.** Sharing ports
  the way clusters do would let the core count rise past the port count, at
  which point §2's exact `D2` saturation stops holding and the roofline moves.
