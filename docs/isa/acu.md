# The accumulator op set

The commands the resident output tile accepts.

Source: [`src/kohakutpu/matmul/mx_acu_fp.v`](../../src/kohakutpu/matmul/mx_acu_fp.v),
[`mx_cluster_node.v`](../../src/kohakutpu/matmul/mx_cluster_node.v) (which wires
it and mixes the two command sources),
[`mx_cluster_mgr.v`](../../src/kohakutpu/matmul/mx_cluster_mgr.v) (which issues
`LOAD` and `ADD`).

Design intent is [`../compute/accumulator.md`](../compute/accumulator.md). This
document is the interface.

---

## 1. The op field

Three bits, presented alongside a tile address and a `cmd_valid` strobe.

| value | name | implemented | effect |
|---|---|---|---|
| 0 | `NOP` | yes | nothing |
| 1 | `LOAD` | yes | tile[addr] = chain |
| 2 | `ADD` | yes | tile[addr] += chain |
| 3 | `ADD_PEER` | **yes** | tile[addr] += peer_in |
| 4 | `SEND` | **yes** | peer_out = tile[addr] |
| 5 | `EMIT` | yes | emit_out = fp16(tile[addr]) |
| 6 | `FWD` | **no** | peer_out = chain |
| 7 | `ADD_EMIT` | yes | tile[addr] += chain, **and** emit_out = fp16(result) |

> **`ADD_PEER` and `SEND` became reachable on 2026-08-10; `FWD` did not.**
> `mx_cluster_node` used to instantiate `mx_acu_fp` with `peer_in` tied to zero
> and `peer_out`/`peer_valid` open, so all three were dead code that compiled —
> `SEND` and `FWD` drove nothing and `ADD_PEER` added zero. It now drives
> `peer_in` from an inbound `CU_DATA` stream (`buf = 2`) and issues `SEND`
> instead of `EMIT` when a drain's sink is a peer, which is what makes a matmul
> able to span clusters at full accumulator precision rather than through an
> FP16 round trip via memory. [cluster.md](cluster.md) §9.4 and §10.1.
>
> **`FWD` still has no command source**, and `mx_cluster_cu` still leaves
> `mx_cluster_node`'s own `peer_out` port open: a send leaves through the drain
> queue as two 256-bit granules, not on a direct ACU-to-ACU wire. So the *direct*
> chain `FWD` exists for is unbuilt, and a K-split across clusters through it is
> the thing the matmul-only mesh maps explicitly do not attempt
> ([`../interlink/topology.md`](../interlink/topology.md) §6.2).

### 1.1 `ADD_EMIT`, and why the port needed it

Two sources drive the port and they are muxed by an **explicit** drain:

```
   sweep    manager, one command per part_valid, via the ordering FIFO
   drain    drain sequencer, one EMIT per sub-tile
```

The mux is a hard switch, not an arbiter. `DRAIN` takes the control port the
cycle it starts, which is why `gemm_busy` has to cover the whole in-flight
cascade — see [cluster.md](cluster.md) §4.7.

That is also why a drain could never overlap a sweep: **the sweep uses every
command slot there is**, one per cycle for `gm*gn*nk` cycles. There is no room
to interleave `EMIT`s, and taking the mux mid-sweep would discard the cascade.
Measured, the serialised drain was 24% of the machine's time.

`ADD_EMIT` removes the need for the slot rather than finding one. Stage 5
already computes a sub-tile's finished value on its last accumulation, so the
emit is the value that is being written back anyway — same command, same cycle,
no second read. In the pipeline it is `ADD`'s operand selects with `EMIT`'s
output, which is why it costs one term in three expressions and nothing in
stage 3, the path that closes timing.

The explicit `EMIT` stays: it is what an output tile drained without a
completing sweep still needs, and it is the fallback for the one shape
`ADD_EMIT` cannot serve (a sweep whose last K block is also its first, which
would need a `LOAD_EMIT`).

## 2. The command FIFO, and why there is one

The cascade has ~19 cycles of latency and it is a function of the TCU count and
the skew SRLs. A matched delay line here would duplicate that constant and rot
the moment the chain changes.

Instead each issue pushes `{op, addr, sa, sb, anchor}` into a 64-deep FIFO and
every `part_valid` pops one. Order is preserved by construction, so alignment
holds whatever the latency turns out to be.

Depth 64 against a ~19-deep chain: this must never fill, because a full FIFO
would silently drop a command and corrupt one output element. Both overflow and
underflow have simulation-only `$display` checks.

## 3. The accumulator format

`ACC_MW + 8` bits per element, 16 elements per sub-tile.

| bits | field |
|---|---|
| `[MW+7]` | sign |
| `[MW+6:MW]` | exponent, 7 bits, bias 63 |
| `[MW-1:0]` | mantissa, implicit leading 1 |

All-zero is the zero encoding. At the default `ACC_MW = 14` that is a 22-bit
float — call it FP22 — and a sub-tile is `16 * 22 = 352` bits.

`ACC_MW` is the only tunable: 16 gives FP24, 12 gives FP20. E7 is fixed, because
range is not the constraint — MW=14 measures identically to MW=16 and costs
less, so 14 is the default.

The partial sums arriving from the chain are 8 chains x 48 bits = 384 bits, two
packed fixed-point fields per chain. `VW = 22`, not 29: a K=32 block can only
reach ±131,072, which is 18 bits and a sign. A 29-bit normaliser would build a
29-bit leading-one search and shifter per lane, sixteen times, for range that
cannot occur.

## 4. Applying the block scale

Two E5M3 fields arrive with every command — `sa` and `sb`, 4 lanes packed into
32 bits each, `{E[4:0], M[2:0]}` per lane. The product of two scales has to be
applied to the partial sum.

**The exponent halves add. The mantissas multiply.**

```
   (1 + Ma/8)(1 + Mb/8) = (m8a * m8b) / 64,    m8 = 8 + M,   m8a*m8b in [64, 225]
```

So the partial sum is multiplied by the 8-bit integer `m8a * m8b`, and the `/64`
comes off the exponent:

```
   val   = part * (m8a * m8b)
   exp   = ea[i] + eb[j] - anchor - 6
```

That is exact. No shifter, no rounding, and no precision lost — which is the
whole reason to split it this way rather than converting each scale to a float
and multiplying. The product is 8 bits wider than the partial sum, which is what
`VWM = VW + 8` is for.

`anchor` is `2*SBIAS = 40`, cancelling the bias stored in both operands'
exponent fields. The `-6` undoes the `/64` the mantissa product carries.

> The mantissa product is declared as its own 8-bit wire. Written inline as
> `{1'b0, ma[i]*mb[j]}` the multiply is self-determined to its 4-bit operand
> width inside the concatenation, so `8*8 = 64` truncates to 0 — silently, and
> every result comes out zero.

**The magnitude is taken inside this multiply, not after it.** `mm = m8a*m8b`
is unsigned, so the product's sign is the chain value's, and

```
   |v + r| * mm  ==  ((v ^ {W{s}}) + (s ^ r)) * mm,       s = v[W-1]
```

which is the same `(v + bit) * mm` shape plus one XOR level, and maps onto the
DSP's `(D+A)*B` mode. So `mx_fpacc_norm_a` receives an **unsigned** magnitude
and the sign travels beside the data. Taking it after the multiply instead put a
30-bit two's-complement carry chain between the DSP's output register and the
leading-one search, which is what §5's frequency used to be limited by —
[`../compute/accumulator.md`](../compute/accumulator.md) §4.4.

## 5. The pipeline

```
   1    extract the two packed fields per chain, apply the scale product
   2a   leading-one search and shift
   2b   round and assemble -> accumulator float
   3    read the tile, compare exponents, align          <- reads the tile
   4    add, leading-one search, shift
   5    round, assemble, write back                      <- writes the tile
   6    (EMIT only) convert to FP16
```

Depth is not the constraint; throughput is. Every stage outside the accumulate
loop is a pure feed and can be split freely. Only the loop itself — stage 3 read
to stage 5 write — costs anything to lengthen, and what it costs is banks.

**One bank.** A pipelined adder cannot close a single-cycle accumulate loop. When
K was the inner loop that was the common case, and it forced three rotating banks
plus a fold on `EMIT`. Sweeping K outermost makes an address recur every `Gm*Gn`
cycles, so there is no tight recurrence left, the loop can be as deep as the
memory wants, and one bank suffices. That deletes the banks, the fold, the zero
mask and three quarters of the tile memory.

The tile is a named `block` memory with `READ_LAT = 2`, using the block RAM's own
output register. Without it the path starts at the RAM array access (~1.2 ns
clock-to-out) instead of a flip-flop, which cost ~70 MHz.

This block closes the whole CU's critical path at **325.6 MHz**, WNS +0.155 ns
against a 3.2258 ns target — out-of-context, so an upper bound rather than a
placed result. Three rules protect stage 3 and breaking any of them costs
20–60 MHz: nothing combinational in front of the tile address (every select that
steers stage 3 is registered a stage early), one mux level on the operands
rather than a chain of ternaries, and zero-ness travelling as one control bit
per operand rather than as a mask on the 384-bit data.

Which stage is tightest *today* was not re-extracted from the latest run. The
last path fixed inside this block was not stage 3 at all but the normaliser's
magnitude chain, ahead of stage 2a — see
[`../compute/accumulator.md`](../compute/accumulator.md) §4.4 and §4 below.

## 6. `REUSE_MIN` — the pacing contract

**Consecutive commands to the same tile address must be at least `REUSE_MIN = 5`
cycles apart.**

Removing the banks turned this from a structural guarantee into a requirement on
the caller, so it is checked in simulation rather than left implicit: a caller
that sweeps K on the inside fails loudly instead of quietly accumulating into
stale data. The check keeps `REUSE_MIN` addresses of history with a separate
valid bit per entry — a sentinel address would collide with a real one, and
`{TAW{1'b1}}` produced a stream of false violations on a design that was correct.

The manager guarantees it by construction: K outermost gives `gm*gn` cycles
between reuses, and it inserts idle cycles below `gm*gn = 5`
([cluster.md](cluster.md) §4.4). The check caught a real violation the moment it
existed — `mx_matmul_cu` emitted a tile 2–3 cycles after the last accumulate into
it.

`busy` covers more than "a command is in the pipeline". It has to mean "not safe
to take the control mux yet", and taking the mux means issuing an `EMIT` that
reads an address the in-flight command may be about to write. So `busy` covers
the write **and** the `REUSE_MIN` gap after it. A pipeline-only version reads
correct and fails only when the whole GEMM is short enough that its tail has not
cleared when `DRAIN` asks; every sub-tile then drains as zero, which looks like a
compute bug.

## 7. `EMIT`

`EMIT` reads the tile, passes it through the align stage against a zero operand,
and converts to FP16 in stage 6. It writes nothing back — the tile survives the
drain.

During a drain, `mx_cluster_node` forces `sa`, `sb` and `anchor` to zero, so no
scale is applied. The value emitted is the accumulator float converted directly.
This is why `DRAIN`'s `anchor` field has no effect
([cluster.md](cluster.md) §5).

The output is one 256-bit word: 16 FP16 values, one 4x4 sub-tile, element
`(i, j)` at bits `[(i*4 + j)*16 +: 16]`.

`emit_valid` is a flag and must be waited on, not counted.

## 8. Capacity

| limit | value | what happens past it |
|---|---|---|
| `DEPTH` (resident sub-tiles) | 512 in `mag_driver_tb`, 256 default | tile address truncates to `$clog2(DEPTH)` bits and wraps |
| command FIFO | 64 | a dropped command corrupts one element; `$display` in sim |
| `REUSE_MIN` | 5 cycles | accumulation onto a stale tile; `$display` in sim |

`TAW` is derived from `DEPTH`, never passed in. As two independent parameters
they silently disagreed: `DEPTH=512` with `TAW=4` addressed 16 entries and
synthesis correctly optimised the other 496 away, with no error anywhere.
