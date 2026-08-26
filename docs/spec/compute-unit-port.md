---
title: Compute-unit port
summary: Every signal a compute unit presents to the mesh and to the framework, every obligation it must meet, and everything it may never do.
tags:
  - spec
  - normative
  - compute-unit
---

# Compute-unit port

> **Kind: Fixed**, except where a section says otherwise. Every signal, bit
> position and handshake rule below is protocol. §10 is illustration and carries
> no obligation.

This is the contract. A unit that meets everything here attaches to any mesh the
framework generates, is discovered by the driver without a hardcoded map, and
cannot deadlock the network. A unit that misses one of the MUST clauses will
usually pass simulation and hang or corrupt on silicon, because most of them
protect against a lost or duplicated flit rather than against a wrong value.

There are two interfaces, and they are not the same thing:

- **The mesh-facing port** (§1–§2). Six signals. This is what the router sees.
  Every endpoint on a mesh presents exactly this, including the memory agent and
  the orchestrator.
- **The datapath handshake** (§3–§6). What `noc_cu_base` offers a unit that lets
  it hold the mesh-facing port. A unit that instantiates `noc_cu_base` inherits
  conformance on the mesh-facing side by construction.

A unit MAY implement the mesh-facing port directly and skip `noc_cu_base`. It
then owes every obligation in §7 itself, including the ones the base module
currently discharges: `CU_CTRL` replies, completion signalling and reply
addressing. Nothing else in the framework will notice the difference. This is
not recommended, and no unit in the tree does it.

### What this document does not constrain

Everything on your side of the datapath handshake. In particular, **the unit's
local memory system is entirely yours**: how many buffers, how wide, in which
primitive, at what read latency, and how they are banked, double-buffered or
addressed. The framework has no opinion and no default here.

The two units in the reference project agree on none of it:

| | operand storage | separate memories | read latency |
|---|---|---|---|
| matmul cluster | 928 bits wide, one RAM per operand side | five: four in the compute nodes, one accumulator tile | 1 for the operand RAMs, 2 for the accumulator |
| vector core | 256 bits wide, one RAM | two: the scratchpad, plus an instruction memory in LUT RAM; the register file is three mirrored RAMs behind that | per-primitive |

Both are conformant. A specification that fixed a buffer width would have made
one of them impossible.

Two things do reach across the boundary and are covered here:

- **How much you can have outstanding**, which the receive queue bounds (§8.2).
- **What arrives, and in what shape** — the memory agent delivers in entries with
  per-entry tagging whatever your storage looks like. That is a Convention it
  effectively forces; see [memory-protocol.md](memory-protocol.md) §3.2.3.

## 1. The mesh-facing port

Reference: `src/kohakuaccel/noc/endpoint/noc_cu_base.v`, `src/kohakuaccel/noc/router/noc_inport.v`,
`src/kohakuaccel/noc/router/noc_outport.v`.

| Signal | Direction | Width | Meaning |
|---|---|---|---|
| `clk` | in | 1 | The mesh clock. |
| `resetn` | in | 1 | Active low, **synchronous** to `clk`. |
| `noc_in_data` | in | `FLIT_WIDTH` | Inbound flit. Meaningful only while `noc_in_valid`. |
| `noc_in_valid` | in | 1 | A flit is offered this cycle. |
| `noc_in_busy` | out | 1 | This endpoint cannot take a flit this cycle. |
| `noc_out_data` | out | `FLIT_WIDTH` | Outbound flit. |
| `noc_out_valid` | out | 1 | A flit is offered this cycle. |
| `noc_out_busy` | in | 1 | The router cannot take a flit this cycle. |

The signal names, widths and the `KohakuNoCPort` bus interface are the same on
every port of `NoCRouter` — north, east, south, west and local. An endpoint is a
local port and nothing else; there is no separate endpoint protocol.

`resetn` on the endpoint is active low and synchronous. **The router's own reset
is not**: `NoCRouter`, `InPortSwitch` and `OutPortSwitch` take `rst`, active
high, and `InPortSwitch`/`OutPortSwitch` use it asynchronously
(`always @(posedge clk, posedge rst)`). A mesh top MUST supply both polarities
from one release, and MUST NOT assume the two are interchangeable. This
divergence is real, present in the shipping RTL, and is called out again in
[Known divergences](#9-known-divergences).

## 2. The link handshake: hold until taken

This is the single most important rule in the framework, and both halves are
required.

- A sender **MUST** hold `valid` high and **MUST** hold `data` unchanged until it
  observes a cycle in which the receiver's `busy` is low. It MUST NOT withdraw a
  flit it has offered.
- A receiver **MUST** accept a flit in exactly the cycles where
  `valid && !busy`, and **MUST NOT** accept it in any other cycle.

Neither half works alone:

| Failure | What happens |
|---|---|
| Sender gives up (clears `valid` because `busy` was high) | The flit is destroyed. It was committed at T against a receiver that raised `busy` at T+1. |
| Receiver accepts on "is there room" rather than on `valid && !busy` | The flit is duplicated, once per cycle of backpressure, because the sender is still holding it. |

Both faults are silent. A duplicated `MEM_WR_DATA` overruns its write slot and
the surplus beat matches nothing; a dropped one leaves the slot short forever, so
the slot never completes, the source's next descriptor opens a second slot, and
that one binds to the older data. The symptom appears several modules away as a
short burst or a wrong tile.

`busy` is a plain *full* signal, not an early margin. `sync_fifo` passes
`USE_ADV_FEATURES(0)`, so XPM ties `prog_full` low and `wr_almost` reduces to
`wr_busy`. What makes that safe is the retry above, not a margin. Any endpoint
that needs a real margin **MUST** count for itself, as `mag_mem_port` does with
`Q_MARGIN`.

Three further rules:

- `noc_in_busy` **MUST** be a function of the endpoint's own state only. It MUST
  NOT depend on `noc_in_valid` or on any field of `noc_in_data`. Deciding
  backpressure from the incoming flit's type means a flit the endpoint cannot
  classify right now blocks the port — and, the mesh being in-order behind it,
  blocks everything else on that link including the flit that would free the
  resource.
- An endpoint **MUST NOT** hold `noc_in_busy` high indefinitely. Every condition
  that raises it MUST be cleared by something other than an inbound flit.
- An endpoint **MAY** assert `noc_out_valid` and `noc_in_busy` in the same cycle.
  The link is full duplex and the two directions are independent.

## 3. The datapath handshake

`noc_cu_base` holds the mesh-facing port and offers these. Reference:
`src/kohakuaccel/noc/endpoint/noc_cu_base.v`.

| Signal | Direction (unit's view) | Width | Meaning |
|---|---|---|---|
| `inst_flit` | in | `FLIT_WIDTH` | The whole `CU_INST` flit at the head of the instruction FIFO. |
| `inst_valid` | in | 1 | An instruction is available and may be accepted. |
| `inst_ready` | out | 1 | The unit accepts it this cycle. |
| `exec_done` | out | 1 | One-cycle pulse: the accepted instruction has retired. |
| `exec_result` | out | 32 | Sampled on `exec_done`. Becomes the `CU_SIGNAL` argument. |
| `exec_fault` | out | 1 | Sampled on `exec_done`. Turns the completion into `SIG_FAULT`. |
| `dbg_ctr` | out | 64 | Free-running unit-defined counters, published as `CU_CTRL` index 3. |
| `send_flit` | out | `FLIT_WIDTH` | A flit the unit wants to transmit, header included. |
| `send_valid` | out | 1 | It is offered this cycle. |
| `send_ready` | in | 1 | The base takes it this cycle. |
| `recv_flit` | in | `FLIT_WIDTH` | The head of the receive queue. |
| `recv_valid` | in | 1 | It is available. |
| `recv_ready` | out | 1 | The unit takes it this cycle. |
| `inst_space` | out (of the base) | 16 | Free entries in the instruction FIFO. |
| `busy` | out (of the base) | 1 | `in_flight` or instructions queued or completions unsent. |

`inst_space` and `busy` are produced by the base for the mesh top and the
`CU_CTRL` block. A unit MAY leave them unconnected.

### 3.1 What the base does with the inbound stream

Every inbound flit is classified by its `type` field and goes to exactly one
place:

| Type | Destination |
|---|---|
| `CU_INST` (`0x5`) | The instruction FIFO, depth `INST_DEPTH`. |
| `CU_CTRL` (`0x7`) | Answered by the base. **It never reaches the unit.** |
| everything else | The receive FIFO, depth `RECV_DEPTH`, presented as `recv_*`. |

`noc_in_busy` is asserted when **either** FIFO is full, not the one the arriving
flit would enter. That is deliberate: `busy` has to be meaningful in cycles when
`noc_in_valid` is low, and the type field is only trustworthy alongside a valid
flit.

Consequence a unit MUST plan for: a receive queue the unit stops draining will
stall the instruction stream as well.

### 3.1.1 `CU_CTRL` is outside that backpressure, and a second one is lost

`noc_in_busy` covers the instruction and receive FIFOs and **nothing else**. A
`CU_CTRL` flit enters neither, so the port never raises `busy` on its account and
never refuses one.

The base holds exactly **one** pending reply. A `CU_CTRL` flit arriving while a
reply is still pending is taken off the link, its index discarded, and no reply
is ever generated for it. The pending reply is unaffected. Nothing is reported,
on the wire or in simulation.

> **A controller MUST NOT have more than one `CU_CTRL` read outstanding to one
> unit.** It MUST wait for the reply before issuing the next request to that
> node.

This is a **requester-side** obligation, unlike the rest of this document, and it
is stated here because this is the page that describes the endpoint that drops
the flit. The failure it prevents has no diagnostic at all: the second requester
waits forever for an answer to a flit the endpoint consumed and threw away, and
no counter moves, no fault is raised, and the link stays healthy. A controller
enumerating a mesh in parallel across nodes is safe; enumerating one node in
parallel with itself is not.

The same rule is stated from the register side in
[control-registers.md](control-registers.md) §1.1.

### 3.2 What the base does on the outbound side

Three producers share the one outbound register, in strict priority:

1. **`CU_SIGNAL`** from the completion queue. Highest, because a completion
   returns the dispatch credit; starving it stalls the orchestrator.
2. **`CU_CTRL` replies**. A controller may be blocked on discovery.
3. **`send_*`** from the unit. `send_ready` is low whenever either of the above
   has something pending.

The base transmits `send_flit` **verbatim**. It does not stamp, rewrite or
validate any header field. The unit owns the entire flit it sends, source
coordinates included.

## 4. Instruction issue and retirement

The framework issues one instruction at a time. `inst_valid` is
`!inst_empty && !in_flight && !sig_full`: a second instruction is not offered
until the previous one has retired *and* there is room to queue its completion.

The unit's obligations:

- The unit **MUST** capture everything it needs from `inst_flit` in the cycle it
  asserts `inst_ready`. The head advances afterwards and the unit MUST NOT rely
  on it persisting.
- `inst_ready` **MUST** be high for exactly one cycle per accepted instruction. A
  unit that drives it from a register MUST guard the accept — both reference
  units test `inst_valid && !inst_ready` for this reason.
- The unit **MUST** assert `exec_done` exactly once for each accepted
  instruction. Never twice; never zero times.
- The unit **MUST NOT** assert `exec_done` in the same cycle as `inst_ready`. The
  base clears `in_flight` on that arm, so the newly accepted instruction's own
  completion would find `in_flight` low and never be queued — a permanently lost
  dispatch credit. Leave at least one cycle between them.
- An `exec_done` asserted while no instruction is in flight is **discarded**. It
  produces no `CU_SIGNAL` and no credit. It **is** still counted: the base's
  retired-instruction counter (`CU_CTRL` index 2) increments on every `exec_done`
  without testing `in_flight`, so a unit that pulses spuriously reports more
  retirements than completions and the two can only be reconciled against the
  orchestrator's own count.
- The unit **MUST NOT** accept an instruction it cannot retire in bounded time
  without further external input that is not guaranteed to arrive.

What the framework does for the unit, so the unit MUST NOT do it itself:

- Remembers `src_x`, `src_y`, `txn` and `last` of the `CU_INST` flit, and
  addresses the completion back to whoever sent the instruction. **A unit never
  needs to be told where its orchestrator is.**
- Chooses the completion code:

  | Condition at `exec_done` | Code sent | Argument |
  |---|---|---|
  | `exec_fault` | `SIG_FAULT` (`0x04`) | `exec_result` |
  | `last` set on the `CU_INST` flit | `SIG_BATCH_COMPLETE` (`0x01`) | `{24'd0, txn}` of that instruction |
  | otherwise | `SIG_INST_COMPLETE` (`0x00`) | `exec_result` |

- Queues completions rather than holding one. The queue is 16 deep — a
  `localparam` inside `noc_cu_base`, not a parameter, so it is the same in every
  build and a unit may rely on it. A unit that retires faster than a congested
  link drains does not lose credits.

A unit **MUST NOT** emit `SIG_INST_COMPLETE`, `SIG_BATCH_COMPLETE` or `SIG_FAULT`
on its own `send_*` path. It **MAY** emit other `CU_SIGNAL` codes — both
reference units emit `SIG_DATA_RECEIVED` (`0x03`) themselves, because a received
burst is not an instruction and nothing else would report it.

## 5. The receive path

- `recv_valid` is `!recv_empty`. The unit pops on `recv_valid && recv_ready`.
- `recv_ready` **MAY** be combinational in `recv_flit`. A unit that needs to
  dispatch by type must make it combinational: registering it on unit state
  alone accepts a flit class the state was not expecting and discards it.
- The unit **MUST** treat a flit as consumed once `recv_valid && recv_ready`
  holds. There is no way to push it back.
- A flit of a type the unit does not understand **MUST** be accepted and dropped,
  not held. Held, it sits at the head of the receive FIFO, raises `noc_in_busy`
  for good, and wedges the instruction stream behind it. Reporting the drop in a
  simulation-only `$display` is **SHOULD**; silent loss is the whole hazard of
  dropping.
- `MEM_WR_ACK` (`0x3`) is the specific case of the above that every writing unit
  hits. Nothing consumes it. A unit that issues writes **MUST** dispose of the
  acks, either by dropping them out of `recv_*` or by diverting them ahead of the
  base, as `mx_cluster_cu` does by gating `noc_in_valid` and forcing
  `noc_in_busy` low for that type.

### 5.1 The bounded-coupling rule

A unit **MAY** hold `recv_ready` low while it waits for something else — both
reference units hold it low while a `SIG_DATA_RECEIVED` is waiting for the link,
so a second burst cannot overwrite the one being reported.

It **MUST** be bounded, and the test is exact:

> Every condition that holds `recv_ready` low MUST be clearable by the send path,
> a timer, or unit-internal progress. It **MUST NOT** require another inbound
> flit.

A unit whose receive path waits on an inbound flit deadlocks the mesh, not just
itself: `noc_in_busy` goes high, the link stalls, and in-order delivery means
everything behind it on that link stalls too.

## 6. Reset

- `resetn` is active low and synchronous to `clk`.
- While `resetn` is low the unit **MUST** hold `send_valid` low and **MUST NOT**
  assert `inst_ready` or `exec_done`.
- The mesh **MUST** hold `resetn` low long enough for the XPM FIFOs inside the
  endpoint to complete their own reset. `sync_fifo` folds `wr_rst_busy` into
  `wr_busy`/`wr_almost`, so the port asserts `noc_in_busy` for the whole
  recovery and no flit is lost — but a sender that ignores `busy` during that
  window still loses one.
- The base's `CU_CTRL` counters (`ctr_inst`, `ctr_busy`) are cleared by `resetn`
  and **by nothing else**. There is no counter-clear register. A measurement is
  the difference between two reads.
- `dbg_ctr` is whatever the unit drives. A unit with nothing to report **MUST**
  tie it to zero rather than leave it floating; the base publishes it unchanged.

## 7. Obligations if you do not use `noc_cu_base`

A unit implementing the mesh-facing port directly owes, in addition to §2:

- **MUST** answer `CU_CTRL` reads at indices 0–3 with the layouts in
  [control-registers.md](control-registers.md) §1. Discovery is how the driver
  sizes itself; a node that does not answer reads as absent.
- **MUST** send exactly one `CU_SIGNAL` per retired instruction, addressed to the
  `src` of that instruction's flit, with `txn` echoed and `last` set.
- **MUST** expose instruction-FIFO free space, so a dispatcher can hold credit
  against it. Backpressuring `CU_INST` into the mesh is the protocol deadlock the
  credit scheme exists to prevent.
- **MUST NOT** let `CU_CTRL` traffic reach a path that can be blocked by the
  datapath. A controller enumerating an unresponsive mesh is how a bring-up is
  debugged.

## 8. What a unit may and may not assume

### 8.1 May assume

| Guarantee | Basis |
|---|---|
| Flits between one `(src, dst)` pair arrive in the order they were sent. | XY dimension-order routing gives exactly one path per pair. |
| A flit offered on `noc_in_*` is never lost, provided the unit honours §2. | Hop-by-hop retry. |
| `CU_INST` flits are delivered to the datapath in arrival order. | The instruction FIFO. |
| `CU_CTRL` never reaches the datapath and is answered whatever the datapath is doing — **provided the controller keeps only one outstanding**. | The base answers it. §3.1.1. |
| Completions are emitted in retirement order. | The completion queue is a FIFO. |
| At most one instruction is in flight. | `inst_valid` is gated on `!in_flight`. |
| The source coordinates on an inbound flit identify the sender uniquely and are preserved across an inter-mesh crossing. | The interlink does not rewrite them. |

### 8.2 May NOT assume

| Non-guarantee | Consequence for the unit |
|---|---|
| Any ordering between flits from **different** sources. | Two senders' bursts interleave. A unit that frames a multi-flit stream by position rather than by source will splice them. Frame by type, and check the source. |
| That a multi-flit message arrives contiguously. | Another node's flit can land between a descriptor and its data. A data flit MUST be identifiable **by type**, never by position. |
| Any bound on the latency of a response, or that one arrives at all. | Nothing in the mesh retries at the message level. |
| That `send_ready` will be high in any particular cycle. | Signals and `CU_CTRL` outrank the unit. Hold and retry. |
| That `noc_out_busy` is low. | Same. |
| That a `MEM_WR_ACK` will be consumed by anyone. | Acks are fire-and-forget; see [memory-protocol.md](memory-protocol.md) §6. |
| That two senders will not target the same unit at once. | If the unit can only reassemble one stream at a time, that is a **unit-level contract it must publish and check**, not something the mesh enforces. |
| That the receive FIFO is deep enough for the requests the unit issued. | The unit MUST bound its own outstanding requests against `RECV_DEPTH`. |

### 8.3 Must never do

1. **Never withdraw an offered flit.** §2.
2. **Never accept a flit outside `valid && !busy`.** §2.
3. **Never make `noc_in_busy` a function of the arriving flit.** §2.
4. **Never block indefinitely on the receive path.** §5.1.
5. **Never hold a flit of an unknown type.** §5.
6. **Never assert `exec_done` in the same cycle as `inst_ready`.** §4.
7. **Never retire an instruction more than once, or not at all.** §4.
8. **Never emit the framework's own completion codes.** §4.
9. **Never issue more outstanding requests than the receive path can absorb.**
   A requester that cannot absorb its own responses converts local backpressure
   into a network-wide stall — the one thing hop-by-hop flow control does not
   solve.
10. **Never rely on cross-source ordering.** §8.2.

## 9. Known divergences

Recorded because the RTL and the surrounding material disagree, and the RTL wins.

| Divergence | Detail |
|---|---|
| Reset convention | `noc_cu_base`, `mag` and `noc_orchestrator` take `resetn` (active low, synchronous). `NoCRouter`, `InPortSwitch` and `OutPortSwitch` take `rst` (active high) and the two switches use it **asynchronously**. One mesh, two conventions. |
| `noc_cu_null` type code | `src/kohakuaccel/noc/endpoint/noc_cu_null.v` declares `T_CU_DATA = 4'h4`. That value is `MEM_WR_DATA`. The correct code is `0x8` (see [flit-format.md](flit-format.md) §3). The null unit only sends to another null unit, so nothing has broken, but the constant is wrong. |
| `CU_CTRL` map | An earlier pre-reframing snapshot lists byte offsets `0x00/0x04/0x08/0x0C` and registers `CU_CONTROL` and `CU_ERROR`. The RTL uses word **indices** 0–3 and the last two are counters. [control-registers.md](control-registers.md) §1 documents the silicon. |
| Instruction FIFO depth | The same snapshot mandates depth 512 in block RAM. `INST_DEPTH` defaults to 32 and every instantiation in the tree leaves it there. |
| Forked base module | `src/reference/poc/noc_cu_base.v` is a divergent copy carrying an extra `ASYNC` parameter and a `clk_noc` port. Nothing in the tree references it. The contract above describes `src/kohakuaccel/noc/endpoint/noc_cu_base.v`. |

## 10. Example: how KohakuTPU's units hold this contract

> **Kind: Convention.** Illustrative only. Nothing below is required of a
> conformant unit, and none of it is forced by the memory agent.

| Obligation | `mx_cluster_cu` | `vec_cu` |
|---|---|---|
| Unknown types | Drops them out of `recv_*`, and additionally diverts `MEM_WR_ACK` ahead of the base by gating `noc_in_valid` and forcing `noc_in_busy` low for that type. | Drops them out of `recv_*`. |
| Bounded coupling (§5.1) | `recv_ready` is low while a peer sub-tile or a `SIG_DATA_RECEIVED` is pending — both cleared by the send path. | `recv_ready` is low while `sg_pend`, cleared by the send path. |
| Multi-flit framing | Frames `CU_DATA` by type, and checks each data flit's source against the open stream's. | Same, plus a `last`-versus-count check. |
| Outstanding requests | One `MEM_RD_REQ` descriptor per `FILL`; the receive FIFO is the only bound, applied as backpressure rather than as a guessed constant. | One `VFILL` outstanding; the core holds a second until the first drains. |
| `dbg_ctr` | `{compute_cycles, memory_cycles}`, both free-running. | `{32'd0, kernel_cycles}`, cleared at each `RUN`. |
| `exec_result` | A running count of retired operations. | Kernel cycle count, or the fault code when `exec_fault`. |

The two units disagree about what `dbg_ctr` means and about whether it is
cumulative. That is correct: index 3 is a **unit-defined** register, and the
driver decodes it per `CU_TYPE`.
