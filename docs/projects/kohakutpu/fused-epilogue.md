---
title: The fused epilogue
summary: A cluster drains its accumulator straight into a vector core's L1 instead of DRAM, so a matmul's activation never becomes a buffer — the encoding, the sequencing, and the band where it fits.
tags:
  - kohakutpu
  - compiler
  - kernels
  - noc
---

# The fused epilogue: cluster → NoC → vector core → DRAM

> **Kind: the fusion is Yours; the transport it rides on is Fixed protocol.**
> Draining an accumulator into another unit's L1 rather than through DRAM is this
> project's choice and its own encoding. The unit-to-unit envelope that carries it
> — `CU_DATA`, the `buf_id` namespace and the acknowledgement rule — is Fixed
> protocol and not this project's to vary
> ([spec/flit-format](../../spec/flit-format.md),
> [spec/memory-protocol](../../spec/memory-protocol.md)).

A matmul followed by an activation used to lower to

```
   cluster --DRAIN--> DRAM --VFILL--> vector core --VDRAIN--> DRAM
```

because the compiler asserted that work crossing unit types crosses memory. It
does not. `DRAIN` addresses the memory port **or** a NoC node, and a
node-addressed drain lands in the receiving unit's buffer ([isa.md](isa.md) §10).
The intermediate never has to exist in DRAM:

```
   cluster --DRAIN(dnode=1)--> vector core L1 --VDRAIN--> DRAM
```

That saves a write and a read of the intermediate — `2 * M * N * 2` bytes for an
`M x N` result — plus the whole VFILL half of the vector program.

---

## 1. What the DSL accepts

The elementwise work is written **on the accumulator**:

```python
@kernel
def linear_silu_fused(x=L.In(..., M, K), w=L.In(N, K), y=L.Out(..., M, N),
                      *, gm=2, gn=1, nk=2):
    with units(x.tiles(gm), w.tiles(gn)) as (i, j):
        acc = L.tile(gm, gn, nk)
        for k in loop(x.chunks32(nk)):
            acc += x[i, k] @ w[j, k]
        y[i, j] <<= acc * sigmoid(acc)
```

`acc` is a `cluster.Tile`. Arithmetic on it yields a `vector.Value` over a new
leaf, `vector.Resident(gm, gn)` — "the accumulator itself, however it arrives".
`c[i, j] <<= <Tile>` is still the plain drain; `c[i, j] <<= <Value>` is the fused
one. Nothing else in the surface changes, and both spellings stay legal.

**One trace, two statements.** The `<<=` emits a `drain` carrying `node=True` and
then an `apply` carrying `resident=True`, into the same recorded grid. The
framework splits a stage whose statements span two unit types into one stage per
unit, in statement order — a generic rule, decided by the project's own
`unit_of`, and the only structural change `kohakuaccel` needed.

The fused form is **refused at compile time**, with the two fixes named, rather
than silently rewritten into a temp (§5). Rewriting would have to invent a `temp`
the trace never declared, and a silent fallback to twice the memory traffic is
exactly the quiet degradation this codebase spends its guards on.

## 2. What the cluster emits

One `DRAIN`, with the destination fields `kohakutpu/isa/cluster.py` already
declared and nothing used:

| field | value | why |
|---|---|---|
| `dnode` | `1` | send to a NoC node rather than the memory port |
| `dst_x`, `dst_y` | the paired vector core (§3) | |
| `buf` | `0` | a vector core has **one flat L1**; anything else faults `F_CUDATA` |
| `dflags` | `1` | bit 0 is `signal_on_complete`; without it nothing can sequence the `RUN` (§4) |
| `dack_y`, `dack_x` | `MachineSpec.agent` | zero answers the sending cluster, which drops it |
| `addr` | `peer_word * 32` | a **byte** address either way; the hardware sends `addr[20:5]` as the descriptor's granule `off` |
| `n` | `gm * gn` | sub-tiles, unchanged |
| `dmesh`, `dfin` | `0` | zero is what makes a drain local |

`buf = 0` also picks the accumulator's opcode. `buf` 0, 1 or memory is
`OP_EMIT`, **one 256-bit FP16 sub-tile per granule**; `buf = 2` is `OP_SEND`, the
accumulator's own float in two granules, and is cluster-to-cluster only. So a
drain into a vector core delivers FP16, at the same width and in the same order a
drain into memory would have.

## 3. Which vector core receives, and the layout it gets

`kohakuaccel.dispatch.plan` deals instances round-robin over the nodes of a type,
in `sorted(payloads)` order. The compiler has to name the receiving core *inside
an instruction*, so it must predict that dealing exactly. Both sides call one
function — `deal(keys, nodes)` — and the vector stage carries the coordinate list
the compiler assumed (`Stage.nodes`), which the runtime then dispatches on.
Placing it on "whichever cores are idle" would land the program on a core holding
none of the data, and the failure would be wrong numbers.

**One open stream per receiver.** The mesh interleaves and a receiver holds one
`{buf, off, left}`, so two senders' bursts to one core merge into each other.
There is no arbitration in hardware. The compiler owns it by refusing a grid with
more instances than there are vector cores, so the pairing is **injective**.

The layout costs nothing, and it is worth being explicit about why. A drain
writes sub-tile `t` to `addr + t*32` — one 256-bit word per `4x4` sub-tile, in
the manager's sweep order. A node-addressed drain sends granule `addr/32 + t`
instead. A granule is 32 bytes; a vector core's L1 word is 32 bytes; `offset` is
in granules, so it is the destination L1 address unchanged. Therefore

```
   L1 word (peer_word + t)  =  sub-tile t  =  16 FP16, row-major within the 4x4
```

which is exactly `kohakutpu.layout.Tile`'s word `t` for that instance. **The
epilogue needs no relayout** — elementwise work commutes with any permutation of
the elements — and **the output is unchanged**, so the fused and unfused forms
are numerically comparable and `unpack` needs no change.

## 4. Sequencing the `RUN`

**A peer write is not a `VFILL` retirement.** `VBAR` and `VHALT` wait on
outstanding fills and nothing here issued a request, so a burst arriving
mid-kernel neither satisfies a barrier nor disturbs one. Nor does the sender's
retirement help: a `DRAIN` is finished when the last write has **left the CU**,
not when it lands. And the cluster's flits and the host's `RUN` flit reach the
core from different sources, so dimension-ordered routing orders neither against
the other.

So the `RUN` is sequenced by the host, on the receiver's own answer:

1. Dispatch the cluster stage. Each `DRAIN` carries `dflags = 1`, so the receiver
   answers `SIG_DATA_RECEIVED` (`0x03`, `arg = buf_id`) to `dack` = the
   orchestrator, landing in `NODE_STATUS[receiver]`.
2. Await, on each receiving core's coordinate, the number of acknowledgements its
   sender will produce — `ceil(gm*gn / WBURST)`.
3. Dispatch the vector stage on those same coordinates.

Step 2 is an ordinary `Await` step pointed at a node that was not kicked. `plan`
takes an `acks` argument mapping an instance to `(receiver, count)`, and attaches
the await to the round that kicked that instance's last window: the loader takes
its `NODE_STATUS` baseline per artifact, so a wait placed a round too late would
have the earlier round's acks already in the baseline.

The vector program is everything the temp form does except the fill. L1 is
`[0, span_w)` for the delivered tile and `[span_w, 2*span_w)` for the result,
where `span_w` rounds `gm*gn` up to a whole 8-word chunk. **Constants go into
scalar registers, not into DRAM** — materialising a folded scalar as a
full-length broadcast array and filling it from memory would put back the traffic
the fusion just removed.

## 5. When it does not fit

Compile-time refusals, each naming the condition and the two fixes — retile, or
write two stages with a `temp`:

| condition | why |
|---|---|
| instances > vector cores | one open stream per receiver; the pairing must be injective (§3) |
| `2 * span_w > L1_SAFE`, or in the band `L1_SAFE+1 .. L1_WORDS-1` | a 352–480-word footprint corrupts the output buffer *and reports success*, measured on `ship_3x2` |
| `gm * gn > 256` | `F_LEN`: a `VFILL`/`VDRAIN` walk longer than 256 entries faults |
| a leaf that is neither the accumulator nor a constant | a second operand would need its own fill, and aligning a buffer's `part` against one instance's tile is undesigned |
| a constant landing in an undemonstrated source slot | guessing a selector yields a legal word that computes something else |

The two live constraints pull against each other: the grid shrinks as `gm*gn`
grows, and the L1 budget shrinks as `gm*gn` grows. With 8 vector cores and
`L1_SAFE = 320`, the band is `gm*gn <= 160` with the grid no wider than 8
instances. A `64 x 128` output at `gm=16, gn=32` is one instance and 512 L1 words,
which fails L1; at `gm=8, gn=8` it is 8 instances and 64 words, which passes — and
that is why the fused kernel defaults to a bigger tile than the unfused one.
**Naming the band is the point:** this is a tuning target, not a free win.

## 6. More than one tile

An epilogue may read **several accumulators**, which is what a gated MLP needs:
`up(x) * silu(gate(x))` is two GEMMs and one elementwise pass. A cluster holds
ONE accumulator, so this is not two tiles resident at once — it is two sweeps in
sequence, each drained as it finishes.

Two things make it work. The drains land in **different L1 slots** of the same
core: slot `r` at word `r * span_w`, so the result moves to `N * span_w` and the
per-channel operand after it. And the first drain is **lifted above the sweep
that would clear it** — the compiler emits `sweep, drain, sweep, drain, apply`
rather than the order the source reads in, because the second GEMM's `acc = 0`
destroys the first tile otherwise. That reordering is the correctness argument,
and `test_two_accumulators_drain_into_one_core` pins the statement order.

Every tile must be ONE shape: they land in equal spans, so a mismatch is refused
rather than served with a span that is wrong for one of them. The ack count
follows for free — `_bursts` already sums over an instance's node drains, so two
drains of `gm*gn = 64` produce 16 acknowledgements rather than 8.

The remaining cost is that the sweeps are serial. Overlapping them needs a
second accumulator, which is [hardware-wants.md](hardware-wants.md) §2.

## 7. What simulation settled

**No bench joined a cluster to a vector core** before this. The RTL send side had
existed in `mx_cluster_cu.v` since the destination fields were added and nothing
had ever driven it at a real receiver. `tests/sysnode/mm_mesh_peer_tb.v` closes that
on the `mm_mesh` machine: MAG at (0,1), cluster at (2,1), vector core at (1,0),
agent at (1,1). It produces the same tile twice from the same L1 — once drained
to the agent, once to the vector core, which writes L1 back to DRAM. Equality
needs no float model, so any difference is the transport. **163 checks, 0
errors.** What that establishes:

1. **A `buf = 0` node drain delivers FP16 sub-tiles, one granule each.** The
   bench counts exactly `gm*gn` granules.
2. **The peer image in L1 is byte-identical to the drained image.** §3's layout
   claim is measured, not read.
3. **One `SIG_DATA_RECEIVED` per burst, `ceil(n/WBURST)` of them.** The bench
   sees `ceil(9/8) = 2` descriptors and 2 acknowledgements with `arg = buf_id`.
4. **An epilogue on the delivered tile is exact**, with no fill anywhere.
5. **`dack` must name the agent, and zero is wrong here.** The orchestrator
   credits the signal's **source**, so the count lands on the receiving core's
   slot — but only if the flit is aimed at the orchestrator at all. Left at zero
   it goes to the sending cluster, which drops it. This is why
   `MachineSpec.agent` exists and why a machine without it refuses to compile
   the fused form.

Two things found on the way: **`vec_cvt_acc` is instantiated by nothing** — the
inbound path does no accumulator-width conversion at all, so the module is built
and benched but not wired; and **a drained value at the top of the FP16 range
saturates rather than overflowing** (doubling `0xf8bb` gave `0xfbff`, not an
infinity).

### Still unverified

1. **The orchestrator's coordinate on the shipped board.** `MachineSpec.agent` is
   the field and the driver has to fill it. `Card` does not expose it today, so
   the fused path refuses on hardware until it does.
2. **A rejected burst into a vector core is NOT acknowledged.** `vec_cu.v` sets
   `cd_sig <= cud_flg[0] && !cud_bad`, so a descriptor naming a bad `buf` or an
   L1 range that does not fit faults and is dropped **in silence**. The cluster
   receiver does the opposite — a rejected burst there is still acknowledged.
   Against an equality poll that is a hang rather than an error. The compiler's
   guards keep `buf` at 0 and the range inside L1, so it cannot provoke it; the
   divergence has not been simulated.
3. **Whether a `VDRAIN` may walk a multi-dimensional descriptor.** Several tiles
   per core — the generalisation that lifts the `instances <= cores` gate — needs
   a strided outer dimension, and nothing shows a drain walking more than one.
4. **The `L1_SAFE` band's cause.** 352–480 words is measured-bad and unexplained.
   The gate is copied, not understood.
5. **Silicon.** Everything above is `MODEL=1` behavioural simulation.

Measured on the unit models (`kohakutpu.model`) at `32x64 @ 32x64`,
`gm=4 gn=8 nk=2`, four cores of each kind: 94 instruction flits against 107, no
temp against a 2 KB one, and no folded constant on the card against two 16 KB
broadcast arrays. **Nothing in this comparison has run on silicon** — see *Still
unverified* above.
