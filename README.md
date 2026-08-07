# [WIP] KohakuTPU

![KohakuTPU-Overall arch](https://github.com/user-attachments/assets/d5222f88-692b-46cd-bdbf-0663eb817afc)

An AI accelerator for Xilinx UltraScale+ FPGAs. Compute clusters on a custom NoC
mesh, reaching DRAM through AXI4, targeting `xcvu13p-fhgb2104-2L-e` at **300
MHz**. The DSP48E2 tile is what the whole arithmetic design is built around.

This project is more for fun than for real usage. If anyone is interested in
making it work, PRs are welcome.

**Start at [`docs/README.md`](docs/README.md).**
[`docs/arch-design.md`](docs/arch-design.md) is the machine top to bottom;
[`docs/isa/`](docs/isa/README.md) is the most accurate description of what it
actually executes.

---

## Where it is

A **two-cluster partition runs a real GEMM of arbitrary size, end to end**: the
Python driver uploads FP16 operands and a control program over AXI, writes `GO`,
and reads back FP16 results. Nothing is stubbed but the DRAM controller, which
is an AXI RAM.

```
   matmul datapath        built, exact against both the behavioural model and
                          the real DSP48E2
   FP22 accumulator       327.7 MHz, 5 BRAM36
   2-port cluster         325.6 MHz, 17,521 LUT, 17,612 FF, 272 DSP, 5 BRAM36,
                          0 URAM -- clears 300 by 8.5%
   NoC mesh + routers     built; lossless, in-order, deadlock-free by routing
   MAG (memory gateway)   built: arbiter, AXI master, quantiser, dispatch agent
   main orchestrator      built: a control program of writes and polls
   driver                 built: tiles any GEMM shape, streams it as rounds
   SLR floorplan          not written, and the DSP cascade makes it a
                          correctness requirement rather than an optimisation
   TLB, cache             not started; v1 needs neither
   vector / general units not started
```

Measured, converted at 300 MHz:

| clusters | shape | run cycles | GFLOP/s | % of peak |
|---|---|---|---|---|
| 2 | 256×256×256 | 18,701 | 538.3 | 87.6% |
| 4 | 256×512×256 | 20,647 | 975.1 | 79.4% |
| 8 | 512×1024×256 | 43,382 | 1,856.3 | 75.5% |

> **That table predates the mesh layout change.** Clusters were placed as a
> (row, left-column) pair; the RTL now places one cluster per **column of a
> band**, managers on the outer rows and accumulators inside
> ([`docs/system.md`](docs/system.md) §2.3). Both 2 CU and 8 CU pass end to end
> on the new layout, and 8 CU measures 72.7% of peak where the rows above
> measured 75.7% — the layout costs about three points, paid in routing rather
> than memory service, in exchange for physical locality and simpler program
> planning ([`docs/perf.md`](docs/perf.md) §0.1). Read the rows above as figures
> for the previous topology.

Two clusters peak at 1,024 MAC/cycle = 614 GFLOP/s, so the 256-cube ran at
87.6% of the datapath. The cycle counts are measured; the rates are those cycles
converted at 300 MHz, which the cluster clears out-of-context (325.6 MHz). What
the eight-cluster row is short of is **instruction dispatch serialising across
clusters**, not memory — no memory budget exceeds a third at any cluster count.
Every shape and cluster count is in [`docs/perf.md`](docs/perf.md) §0, with the
fill-bound baseline it started from (6–7% of peak) and why each claim of
"bandwidth-bound" turned out to be wrong.

## The numeric format

Elements are **int7 with an E5M3 scale shared by a block of 32** along the
reduction dimension — a microscaling format in the OCP style, but with two
deliberate departures:

```
   value(i,k)  =  q[i,k] * scale[i]
   scale       =  2^(E - 20) * (1 + M/8)      field = { E[4:0], M[2:0] }
```

**The scale is not a power of two.** An E8M0 scale can only land a block's peak
somewhere in `[32,64)` of the int7 range, so between zero and a full bit of the
significand goes unused, and which it is depends on where the peak happens to
fall inside its binade. Three mantissa bits put the peak at 63 every time.
Measured per element on correlated operands: relative error p50 0.54% → 0.38%,
p99 48% → 23%. The cost is one small multiply at each end, and the field is
still 8 bits, so nothing about the flit format or the buffers changes.

**The exponent is E5, not E8.** The output format is FP16, whose normal range
spans 30 binades; E5 covers 31 and just fits, E4 covers 16 and does not. The
three extra exponent bits an E8M0 field spends buy range this datapath cannot
express anyway.

Software never sees a quantised value. It uploads FP16, `mx_quant.v` converts on
the way out of the memory gateway, and results come back FP16 — so the machine
as a whole is **AMP FP16 with an MXFP7 multiply and an FP22 accumulator**, and
the throughput unit is FLOPS rather than IOPS. `driver/src/kohakutpu/mxfp7.py`
is a *model* of the hardware, used to predict what it will produce so tests can
check it; MXFP8 and the other OCP formats survive only in
`driver/src/kohakutpu/formats.py` as comparison baselines.

Details: [`docs/compute/matmul.md`](docs/compute/matmul.md) §3 and
[`docs/isa/memory.md`](docs/isa/memory.md) §6.

## The shape of the machine

```
   host  --AXI4-->  main orchestrator  --AXI4-->  MAG  ====NoC mesh====  clusters
                    control program              memory + dispatch       compute
```

A **cluster** is four 4×8×4 tensor CUs chained through the DSP48E2 cascade, plus
one accumulator holding the output tile resident. 512 MACs/cycle. The four
tensor CUs are **not** NoC nodes — they are wired to each other by `PCOUT →
PCIN`, which is why they cost zero LUTs: the multiply *and* the entire K=32
reduction happen inside the DSPs.

A cluster takes **two** NoC ports, not five, and the reason is arithmetic. The
chain consumes eight 256-bit operand words per cycle and one port delivers one,
so feeding the tensor CUs directly is an 8× deficit however many ports you
spend. Holding a large output tile resident closes it instead: a cluster
computing a `Gm × Gn` block needs `4(Gm+Gn)/(Gm·Gn)` words per cycle, which at
16×32 is 0.375. One port carries operands, one carries results. **64 ports for
32 clusters, not 160.**

Those two ports sit on **adjacent routers in the same column**: a cluster is one
column of a *band* (two mesh rows), with its manager on the band's outer row and
its accumulator directly beneath. With two bands the second is mirrored, so
accumulators meet in the middle and every manager is on an outer row — managers
outside, accumulators inside, two dataflow rings back to back. MAG hangs off the
west edge with one port per row, leaving north, south and east free for the
vector unit and general core. [`docs/system.md`](docs/system.md) §2.3 draws it.

At 45 clusters the device is DSP-bound — 12,240 of 12,288 DSPs — with LUTs at
46% and BRAM at 8%, giving ~13.8 TFLOPS of peak at 300 MHz. A cluster is 272
DSPs: 256 in the cascade and 16 in the accumulator, which is why the count is 45
rather than the 48 an all-fabric accumulator would have allowed. One cluster is
what has been synthesised; 45 is that measurement multiplied out, not a build.

## Why a custom NoC

An AXI4 interconnect wide enough to feed 32 or more compute units is a crossbar
whose cost grows with masters × slaves, carrying machinery this design never
uses: out-of-order completion by ID, burst reordering, exclusive access. AXI4-
Lite drops all of that and drops the bandwidth with it. What the machine needs
is narrower than either — one clock domain, one-flit messages, mostly
nearest-neighbour traffic — so a mesh built for exactly that is smaller than an
interconnect configured down to it.

288-bit flits, 5-port routers, XY dimension-order routing on clamped
coordinates, busy/valid links and end-to-end credits. XY is acyclic by
construction, so deadlock-freedom is a property rather than a test result.

| Router | 2D mesh |
| ---------------------------------------------- | ---------------------------------------------- |
| ![router](image/README/1735483831920.png) | ![mesh](image/README/1735483805869.png) |

See [`docs/noc/spec.md`](docs/noc/spec.md).

## Running it

```
   python scripts/py/check.py fast     ~5 s     pure Python, no simulator
   python scripts/py/check.py unit     ~70 s    + the benches that catch most
   python scripts/py/check.py full     ~6 min   everything

   python driver/run_matmul.py --m 64 --n 64 --k 128
```

Simulation is Vivado `xsim` — the mesh instantiates `xpm_fifo_sync`, which needs
`-L xpm`, so iverilog is not an option for it. Benches run against both
`MODEL=1` (behavioural DSP) and `MODEL=0` (the real `DSP48E2`), so a failure is
attributable to one or the other. See
[`docs/simulation.md`](docs/simulation.md).

## Layout

```
   src/kohakunoc/    mesh, routers, CU framework, dispatch agent
   src/kohakutpu/    compute: matmul datapath, accumulator, cluster
   src/kohakumas/    memory access gateway, quantiser
   src/kohakuaxi/    AXI4 slave/master, main orchestrator, crossbar
   src/common/       sync_fifo, kohaku_sdpram
   driver/           the Python driver: tiling, control programs, models
   tests/            benches, one per subsystem
   docs/             design intent and measured results
```

## License

This project is still a work in progress. During the WIP state, all source code
and related resources are released under a custom Kohaku-Code-License (or
Kohaku-License if needed), an open-access license with some restrictions on
commercial usage. See the License file.
