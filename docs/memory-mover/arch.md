# Architecture

Where the mover sits, what is inside it, and how each part is sized.

---

## 1. It is a new client of MAG, not a new MAG

**`mag_mem_port.v` is not modified.** MAG gains a client. That distinction is
load bearing: the port is tested, its write slots are sized against a specific
deadlock argument, and its read engine is the thing that once stopped the
machine scaling. None of that should be reopened to add a copy engine.

The precedent is already in `mag.v`, which has a **non-NoC AXI master**:

```
  NoC port 0 ──┬─►  mag_mem_port ──► AXI master 0 ─┐
  NoC port 1 ──┼─►  mag_mem_port ──► AXI master 1 ─┤
       ...     │                                   ├──► memory
  AXI slave, memory ──► upload ────► AXI master N ─┘
  (host, FP16 in)       (quantise on the way in)
```

The host upload path is exactly this shape -- a unit inside MAG with its own AXI
master, no NoC endpoint, walking memory and transforming on the way. **The mover
is master N+1.** And `mag.v` already records that *"the agent has no port of its
own"*, so a unit that lives in MAG and is not a mesh node is an established
pattern here.

**Not being on the mesh is a feature.** It needs no router port and no slice of
the link budget that `../general_cores/README.md` §3 says is what capped the
vector cores at 16.

### 1.1 The delta to MAG

| | |
|---|---|
| `MP1 = MEM_PORTS + 1` | becomes `MEM_PORTS + 2`; one more crossbar leg |
| control decode | one more target on the 64-bit control window |
| `mag_mem_port.v` | **untouched** |

## 2. How it is commanded

Through the **64-bit control AXI window** that `mag.v` already exposes
(`sc_*`). Two callers, one interface:

- **the host**, for bring-up and for the fallback path
- **the agent**, in production, as part of a control program

That is deliberate. A unit that can only be driven by the agent cannot be
brought up before the agent understands it, and this project has repeatedly
been served by having a host-visible path to a new engine first.

Completion is a status register plus a level the agent can wait on. The mover
is a **consumer with the ordinary contract -- descriptor in, completion out** --
reached over local wires rather than the NoC, which is what keeps
[`compiler.md`](compiler.md) simple: a `MOVE` band dispatches like a `GEMM`.

## 3. The datapath

```
  control writes ──► command FIFO
                          │
                 ┌────────┴────────┐
                 ▼                 ▼
          src mx_tdesc       dst mx_tdesc          6 dims each, bound axes
                 │                 │
                 ▼                 │
          read issue ──► AXI AR/R  │
                 │                 │
                 ▼                 │
          ┌──────────────┐         │
          │ granule buf  │◄────────┤   transpose / assembly
          │ index buf    │         │   gather indices
          │ PRNG         │         │   generate
          └──────┬───────┘         │
                 ▼                 ▼
              write issue ──► AXI AW/W/B
```

### 3.1 Reuse `mx_tdesc`, do not invent an AGU

`src/kohakutpu/matmul/mx_tdesc.v` is a **6-dimensional affine walker with two
bound axes**, already built, unit-tested in `mx_tdesc_tb.v`, and instantiated in
`mx_cluster_mgr.v`. It gives, per step, one `(addr, valid)`:

```
  addr    = base + SUM idx[i] * stride[i]
  axis[a] = abase[a] + SUM over i with axis[i]==a of idx[i] * astep[i]
  valid   = for all a:  0 <= axis[a] < aext[a]
```

Three properties matter here and none would be free in a new design:

- **`valid` low is padding.** The caller injects zeros and issues no memory
  request, so `pad` is not a mode -- it is what the descriptor already does.
- **No multipliers.** Each dimension carries its own partial sum, rewound to
  zero on wrap, so the address path is an adder tree. That is what keeps it out
  of the Fmax conversation.
- **Six dimensions.** Enough for the conv2d walk it was built for, and more than
  enough for any layout conversion here.

**Instantiate two: one for the source, one for the destination.** The move is
one loop nest read through two stride lists, which is the whole expressiveness:

| operation | how |
|---|---|
| copy | identical stride lists |
| transpose | swap two strides on one side |
| permute | permute the stride list |
| broadcast | source stride 0 |
| slice | bounds and an offset on the source base |
| concat | two moves into one region at different destination bases |
| pad | destination extent exceeds the source's bound axis; `valid` zero-fills |
| tile order <-> entry order | a fixed stride pattern per side |

## 4. Modes

One field, five values. `isa.md` §3 has the encoding.

| mode | source | destination | note |
|---|---|---|---|
| `COPY` | descriptor | descriptor | the common case; word granular |
| `TRANSPOSE` | descriptor | descriptor | routes through the granule buffer |
| `GATHER` | index buffer + base | descriptor | row granular, §6 |
| `GENERATE` | PRNG | descriptor | no reads at all, [`prng.md`](prng.md) |
| `FILL` | an immediate | descriptor | zeroing a region, and `pad`'s backstop |

`COPY` and `TRANSPOSE` are the same datapath with the buffer bypassed or not.
Keeping them distinct in the encoding is what lets the cost model price them
differently without inferring intent from strides.

## 5. Granularity is the entire performance story

**A mover that is elementwise-general is a mover that is slow.** AXI moves
256-bit words; a 16-bit element at an arbitrary stride is one useful element per
burst. Three regimes, and the compiler must know which one it asked for:

**Word granular, full bandwidth.** The innermost dimension is contiguous and at
least one word long. **Tile order to entry order is in this class** -- both
layouts are built out of 256-bit words, so the conversion is a permutation *of
words*. The most valuable thing the mover does is also its cheapest.

**Granule buffered, full bandwidth, bounded shape.** A true element transpose
cannot be word aligned on both sides. Buffer a granule -- 32 x 32 elements,
2 KB, one BRAM36 -- read 32 aligned rows in, write 32 aligned columns out. Both
sides run at word width. This is how every hardware transposer works.

**Element granular, slow, and visibly so.** Whatever the first two cannot cover.
It must still work, and `compiler.md` §5 must price it honestly. This project
has been burned by plausible wrong *answers*; a plausible wrong *cost* is the
same failure relocated into the scheduler.

Double-buffer the granule (two BRAM36) so read and write phases overlap. That is
the difference between half bandwidth and full on every transpose.

## 6. Gather

A dependent read -- fetch `idx[i]`, then fetch the row at
`base + idx[i] * pitch`. The clean structure is two phases, not a dependent
pipeline:

1. read the whole index vector into the **index buffer**
2. stream row reads, one AR per index, writing rows to the destination

That bounds one gather by the buffer, which is a limit worth having because it
is checkable at compile time. One BRAM36 holds 1024 32-bit indices.

**Gather rows, not elements.** Every workload that motivates this already has
wide rows: an embedding row at `C = 4096` is 8 KB, an MoE token row the same, a
KV-cache entry likewise. Row-granular gather is word aligned on both sides and
runs at bandwidth; element-granular gather is §5's third regime and is priced
there.

Scatter is the same engine with the descriptors exchanged. **Scatter-add is
not** -- see `README.md` §4.

## 7. Worked example: appending to the KV cache

The case that closes with the mover alone, no general core.

A decode step produces `K` shaped `(H, 1, D)`; the cache is `(H, Lmax, D)`.
Appending at `t` writes **H rows, each contiguous in D, separated by
`Lmax * D`**:

```
  src   base = K_out        dims (H, D)      strides (D, 1)
  dst   base = cache + t*D  dims (H, D)      strides (Lmax*D, 1)
```

One descriptor pair. Today this is not one DRAIN, because a drain writes
*contiguous* sub-tiles and this is H bursts at a large stride.

And the conversion rides along: `K` leaves the accumulator in sub-tile order and
attention's FILL wants entry order, so the source strides describe one order and
the destination strides the other. **The append and the relayout are one move.**

## 8. Ordering, and the invariant this breaks

`mag_mem_port.v` states a load-bearing invariant: *"the ports never write the
same word: each owns the C tiles of its own clusters."* **A mover breaks it**,
because a move can write anywhere, and it would present exactly like the
relayout bug -- right bytes, wrong place, no exception.

**The mover is a writer the ports do not know about.** The invariant is not
repaired by partitioning it -- it is repaired by the compiler: a region a move
writes must not be written by a port in the same round. Rounds already express
that, and it is the same discipline that keeps two clusters off one tile.

**A move is not done until its writes have retired** -- not when the last word
enters the write path, but when AXI has returned `B`. Clusters get away with
discarding `MEM_WR_ACK` because nothing reads what they wrote inside the same
round. A move exists precisely so that something reads it next.

**Consumers are ordered by the round barrier that already exists.** A move
issued in round *r* is visible to a FILL dispatched in round *r+1* and no
earlier.

There is no cache and no TLB in v1, which removes coherence entirely. If a cache
ever lands in MAG, this section is where it breaks.

## 9. Sizing, and what each number costs

| block | size | cost | set by |
|---|---|---|---|
| granule buffer | 32 x 32 x 16b, doubled | 2 BRAM36 | which transposes must be fast |
| index buffer | 1024 x 32b | 1 BRAM36 | largest single gather |
| read data FIFO | 32 x 256b | LUTRAM | AXI read latency |
| write data FIFO | 32 x 256b | LUTRAM | AXI write latency |
| two `mx_tdesc` | 6 dims each | ~2 x its own footprint | fixed |

The whole unit is a few BRAM and some control. It is not what competes with the
vector cores for routing.

## 10. Measured

Built, and out-of-context synthesis on `xcvu13p-fhgb2104-2L-e` against a
**320 MHz** target (`.\tests\run_synth_check.ps1 -Only mm_prng,mm_mover -Freq 320`).
The target is 320 because the machine is meant to *run* at 300, and a design
measured at the speed it will be clocked has no margin for placement.

> **What these numbers are, and are not.** `scripts/synth_check.tcl` runs
> `synth_design -mode out_of_context` and stops: no `opt_design`,
> no `place_design`, no `route_design`. Everything below is post-*synthesis*
> with estimated routing. **No place-and-route has been run on this project at
> any scale.** The largest thing ever measured is `mm_mesh` at 5.1% of the
> device; the delivered machine is 16 vector cores plus clusters across several
> SLRs, where crossings and congestion dominate and none of this transfers
> directly. Treat a figure as an upper bound, and treat a result within ~1% of
> target as unresolved rather than as a pass or a fail.

| | `mm_prng` | `mm_mover` (includes the PRNG) |
|---|---|---|
| **Fmax** | **333.0 MHz** | **331.8 MHz** |
| WNS at 320 | +0.122 ns | +0.111 ns |
| LUT | 1,089 | 3,425 |
| FF | 580 | 3,924 |
| BRAM36 | 0 | 4 |
| DSP | 0 | 3 |

Both were stuck at **299.5 MHz** on the same path -- the 32x32 constant-multiply
DSP cascade's output to a fabric register, inside the PRNG. A DSP48E2's
clock-to-out into general fabric is not something a pipeline stage *around* the
multiply can fix, because the multiply itself is the segment. Since `M` is a
constant the product splits into two 16x32 partials with a register between the
partials and their sum, `M*c = M_lo*c + (M_hi*c << 16)`, and at that width
synthesis drops the DSPs entirely and builds the partials in LUTs: **+800 LUT,
-8 DSP, +33 MHz**. The mover's critical path then moved out of the PRNG
altogether, into `mx_tdesc`'s axis compare, with 0.111 ns to spare.

### 10.1 What the timing work actually cost

Three fixes, each measured, and each a pipeline stage rather than a rewrite:

| | before | after |
|---|---|---|
| key bump off the DSP-to-DSP path | 261.0 | 261.0 (no change -- it was not the path) |
| PRNG: a register between multiply and XOR | 261.0 | **299.5** |
| mover: index registered before the multiply | 188.7 | 257.8 |
| mover: product and base add split | 257.8 | 254.2 |
| mover: walker outputs latched | 254.2 | 299.5 |
| PRNG: 32x32 split into two 16x32 partials | 299.5 | **333.0** (mover **331.8**) |

Three of those are worth remembering. **A wide multiply is a segment, not a
stage**: no amount of registering around a 32x32 helps, because the DSP cascade
and its exit into fabric *are* the path -- splitting the arithmetic is the only
move. **The counter registers were being absorbed
into the DSPs' own P registers**, which left the round's XOR sitting directly
between two multipliers with no register anywhere -- the fix is a real pipeline
stage, not a coding change. And **`mx_tdesc`'s `valid` reached `m_araddr`'s clock
enable**: a bounds comparison over the axis accumulators driving a datapath
enable. Latching the walker outputs before the control logic sees them is what
took the mover from 254 to target.

The PRNG is now four cycles per round, 40 per 128 bits. That is deliberate --
`prng.md` §1 establishes that rate is not the scarce thing here.

### 10.2 The assembled machine, and what it found

`src/synth_top/mm_mesh.v` is the minimal machine: MAG (agent, memory port and
mover), one matmul cluster, one vector core, two routers. It is verified end to
end by `tests/mas/mm_mesh_tb.v` -- **22 checks** -- which drives the NoC from
r11's local port and reads the AXI RAM's backdoor: a mover transpose, a vector
kernel drained to DRAM, and a cluster FILL served by MAG.

The vector kernel is the full round trip -- `VFILL` out of DRAM through MAG's
memory port, then `VLD`, `VADD`, `VST` and `VDRAIN` back -- so both directions
of the port are exercised, not just the write.

| | |
|---|---|
| LUT | ~91.7k (5.3% of the device) |
| FF | ~62k |
| BRAM36 | 13 |
| DSP | 400 |

**The mover is not the limiter. The vector core is**, and nothing said so until
the parts were assembled:

| | first measured | now |
|---|---|---|
| `mm_prng`, `mm_mover` | 261 / 189 MHz | **333.0 / 331.8 MHz** |
| `vec_alu` (one lane, measured earlier) | 324.8 MHz | unchanged |
| `vec_lanes` (16 lanes + register file) | 305.1 MHz | **358.4 MHz** |
| `vec_cu` (the whole vector core) | **229.3 MHz** | **336.8 MHz** |
| `mm_mesh` (the whole machine) | **129.2 MHz** | **325.6 MHz** |

The mesh went 129.2 -> 178.5 -> 208.9 -> 248.0 -> 280.6 -> 286.8 -> 318.6 ->
322.0 -> **325.6 MHz**, and not one of those fixes was in the mover.

**Where it stopped is the useful part.** The mesh's critical path is now
`u_cluster/u_node/u_acu/val_r_reg/DSP_M_DATA_INST -> b_sig_reg` -- the matmul
accumulator, at **exactly the 325.6 MHz `compute/accumulator.md` §4.4 measures
for that module on its own**. The assembled machine has converged on a
component's standalone limit, which means the vector core, the mover and the
NoC have stopped contributing: further work on any of them buys nothing until
the ACU moves.

Two of those are worth stating plainly.

**A module can pass alone and fail assembled for a reason that is not
integration.** `vec_lanes` meets 300 MHz standalone because `mode` is a top
input with an ideal driver. Inside `vec_cu` it is a register in `vec_core`, and
`vmode -> width -> slice index -> a 16-way enable decode -> the register file's
write port` is the longest path in the machine. Synthesising a module whose
control inputs are really registers elsewhere flatters it.

**Two multiplier chains in `vec_agu` had never been measured at all.** The
descriptor's `total = b0*b1*b2*b3` ran from a bound register through three
32-bit multiplies into the sequencer's *state* register (129 MHz), and the
address `base + SUM idx_i * stride_i` ran four more into `l1_raddr` (178 MHz).
The first is now pipelined; the second is gone entirely, replaced by the
running-partial-sum technique `mx_tdesc.v` already used and documented -- which
is exactly why `mx_tdesc` carries no multipliers.

### 10.3 The fixes to the vector core

| | | |
|---|---|---|
| `vec_agu` | pipeline `total`; two wait states in `vec_core` before reading it | 129 -> |
| `vec_agu` | replace `idx*stride` with running partial sums -- no multipliers | 178 -> |
| `vec_agu` | latch the SELECTED descriptor on `start`, killing an 8-way mux | |
| `vec_lanes` | decode the write enable **and the crossbar select** one stage early and register both; the data cannot move, the select can | |
| `vec_core` | register VCVT's midpoint -- its round trip was two float conversions in one cycle | -> 286.8 |
| `vec_agu` | saturate `b0*b1` and `b2*b3` to 16 bits, so `total`'s second multiply is 16x16 and fits one DSP instead of cascading | `vec_cu` 294.9 -> 310.4 |
| `vec_core` | register L1's output before the load converters | mesh 286.9 -> 318.6 |
| `vec_core` | select the converter's source a cycle early, so it reads **one** register rather than a mux of two | `vec_cu` 310.4 -> 322.0, mesh -> 322.0 |
| `vec_agu` | give the saturate its own stage, so `total` is three deep | `vec_cu` -> **336.8**, mesh -> **325.6** |

The middle two are the transferable ones. **An N-way descriptor mux between a
register and a multiplier or an adder is invisible until assembly**, and **a
crossbar whose select comes from a control register puts that register on the
datapath's critical path** -- registering the select, not the data, is what
fixes it, and it made the crossbar ~8k LUTs cheaper as well.

The last two are the same lesson from the other side. A block of deep
combinational logic -- here sixteen parallel FP16 normalises -- has no room for
anything else in its cycle, so **whatever selects its input must not be in
series with it**. Registering the BRAM output fixed the source; the mux that
replaced it then cost almost as much, and the fix was to move the *selection* a
cycle earlier rather than to add another stage. `ls_kind` is set at decode and
stable for the whole access, so choosing early chooses the same thing.

A VLD chunk costs one more cycle for this, and a load is not the inner loop.

One trap paid for twice in this module: **splitting an `always @(*)` block
means giving the new block its own loop variables.** Sharing them churns delta
cycles, and the symptom is a simulation that crawls rather than one that fails.

## 11. Faults

A move that cannot be completed must **fault, not truncate**. The status
register carries a code:

| code | meaning |
|---|---|
| 1 | gather length exceeds the index buffer |
| 2 | a descriptor walks outside its declared region |
| 3 | AXI returned a slave error |
| 4 | `GENERATE`/`FILL` given a source descriptor, or `COPY` given none |

Code 2 is the one that matters. The compiler owns allocation, so a descriptor
leaving its region is a compiler bug, and this is the only place it can be
caught before it corrupts a tensor silently.
