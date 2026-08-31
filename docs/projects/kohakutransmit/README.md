---
title: Kohaku Transmit Surface
summary: A credit-based, latency-insensitive transport for FPGA links — on a die, across dies, across chips — with no ready on the wire, any width, and one register-to-register path per stage.
tags:
  - kohakutransmit
  - kts
  - interconnect
  - overview
---

# Kohaku Transmit Surface

> **Standing: a project of its own.** `src/kohakutransmit/` imports nothing from
> any other tree — it carries its own two memory primitives — and anything may
> import it. These pages describe only KTS; what a user does with a surface is
> that user's page.

**Device for every figure in these pages: `xcvu13p-fhgb2104-2L-e`, Vivado
2024.2, out-of-context synthesis at a 3.333 ns ask.** [measurements.md](measurements.md)
carries every number with the run that produced it; the other pages quote only
what they need.

## 1. What it is

A **surface** is one direction of a link: a forward wire that carries flits and
a backward wire that carries credits. Nothing else. There is no `ready` on
either wire: the sender emits a flit only while it holds a credit for that
flit's virtual channel, the receiver's buffer is exactly as deep as the credits
it issued, and a credit goes back for every flit the receiver's consumer takes
out. Because the sender never waits on the receiver *in the same cycle*, the
wire between them may be anything — nothing, a dozen register stages, a die
boundary, a clock crossing, a serial transceiver to another chip — and the only
thing that changes is the round trip the credits have to cover.

```
   sender                          the wire                         receiver
  ┌─────────┐  valid, vc, last, flit[W]  ──────────────────────▶  ┌──────────┐
  │ kts_tx  │                                                    │  kts_rx  │
  │ credits │  ◀──────────────────────  crd_valid, crd_vc, crd_n │ D per VC │
  └─────────┘                                                    └──────────┘
```

Three properties follow, and they are the whole reason the project exists:

| property | what it means on the wire |
|---|---|
| **latency-insensitive** | correctness never depends on the round trip; only throughput does, as `min(1, D / RTT)` flits per cycle per VC, where `D` is the receiver's depth and `RTT` the round trip in cycles |
| **cheap** | a sending end is a credit counter per VC and one output register; a receiving end is a FIFO per VC and a batching counter; a register stage on the wire is `W + VCW + 2` flip-flops and **zero LUTs** |
| **one route per stage** | every signal is a register at both ends of every wire, so a stage's period is one flop-to-flop route and nothing else |

Against a valid/ready channel — AXI4-Stream, or one channel of AXI4 — the
difference is structural: `ready` travels *against* the data in the same cycle,
so every register stage on such a channel is a skid buffer with a combinational
path through it, every clock crossing is a FIFO per channel, and a die crossing
that must be flop-to-flop with nothing in between cannot carry `ready` at all.
A surface has no such signal to carry.

## 2. The layers

| layer | modules | what it adds |
|---|---|---|
| **surface** | `kts_tx`, `kts_rx` | the credited wire; VCs; receiver-issued credits; batching |
| **carriers** | `kts_pipe`, `kts_cdc`, `kts_wconv`, `kts_over_axis`, `kts_over_axi4`, `kts_over_serial` | the wire's length: register stages; a clock crossing; a width change; a surface carried by an AXI4-Stream, by AXI4 posted writes, or by a word stream with framing, sequence numbers and replay |
| **packets** | `kts_pkt.vh`, `kts_switch` | a header flit with kind, destination, source, byte length and tag; a K-port switch routing by destination range, packet-atomic per output and VC |
| **bridges** | `kts_axi4_in`, `kts_axi4_out`, `kts_axis_in`, `kts_axis_out` | AXI4 and AXI4-Stream carried *over* a surface, so an existing AXI endpoint reaches across any wire without knowing it |
| **primitives** | `kts_fifo`, `kts_afifo`, `kts_ram` | the project's own named memories: a first-word-fall-through FIFO, its dual-clock twin, a simple dual-port RAM |
| **reference** | `kts_ref_skid` | the one-stage valid/ready register slice the measurements are made against |

Two directions of bridging are deliberately both present. A bridge carries
**AXI over KTS**: an AXI4 master on one side, an AXI4 slave on the other, the
surface between. A carrier `kts_over_*` carries **KTS over AXI**: the surface
itself is tunnelled through an AXI4-Stream, an AXI4 interconnect, or a serial
stream, and comes out the far end as the same surface. The second is what makes
a surface reach a chip the first cannot: the same flits, the same credits,
whatever is in the middle.

## 3. What a surface costs

From [measurements.md](measurements.md) §2, at W = 288 and two VCs:

| | LUT | FF | note |
|---|---|---|---|
| `kts_tx` | **178** | 306 | the credit counters, the VC arbiter, the output register |
| `kts_rx`, 32 deep per VC in LUTRAM | 446 | 1,540 | the two FIFOs are most of it |
| `kts_rx`, 128 deep per VC in block RAM | **189** | 404 | plus 9 block RAM — the LUT figure of a link end that can cover a 128-cycle round trip |
| one register stage on the wire (`kts_pipe`, N = 1) | **0** | 291 | |
| the valid/ready reference stage (`kts_ref_skid`) | 152 | 581 | the same width; the vendor's fully registered AXI4-Stream slice is 300 / 584 |
| a clock crossing (`kts_cdc`) | 589 | 1,226 | both directions; the vendor's AXI4-Stream clock converter is 260 / 722 for one |
| a width change 288 → 144 (`kts_wconv`) | 699 | 1,704 | a receiving end, the shift, a sending end |
| a K = 3 switch | see [measurements.md](measurements.md) §2.3 | | the return muxes dominate; the arbiters read registers |

The pipe stage is the number the project is built around: **a stage costs
registers only**, so the length of a wire is a question of latency and buffer
depth, never of logic or of a path.

## 4. Where to read next

| page | what it is |
|---|---|
| [spec.md](spec.md) | **the specification**: the wire, the credit rules, the header format, the carriers' contracts, ordering and deadlock, every parameter |
| [manual.md](manual.md) | **the usage manual**: sizing `D` and `W`, choosing VCs, bringing a link up, adding a carrier or a protocol, debugging a credit count |
| [examples.md](examples.md) | **the reference implementations**: a link across register stages, a surface tunnelled through an AXI4-Stream path and through an AXI4 interconnect, AXI4 across a surface, a lossy serial channel with replay, a ring of switches |
| [measurements.md](measurements.md) | **every number**: each layer at four widths, against the vendor register slices and converters |

RTL: `src/kohakutransmit/` — `link/`, `carrier/`, `packet/`, `bridge/`,
`prim/`, `ref/`. Benches: `tests/transmit/`, all in `scripts/py/xsim.py`.
Measurement scripts: `scripts/tcl/ooc_mod.tcl` for the modules,
`scripts/tcl/ooc_kts_ref.tcl` for the vendor references.
