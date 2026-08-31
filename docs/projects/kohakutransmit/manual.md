---
title: KTS usage manual
summary: How to put a surface to work — sizing depth and width, choosing virtual channels, bringing a link up, tunnelling it through what is already there, adding a carrier or a protocol, and reading a credit count when something is wrong.
tags:
  - kohakutransmit
  - kts
  - manual
---

# KTS usage manual

The [specification](spec.md) says what a surface is; this page says what to do
with one. Every recipe here is one of the [examples](examples.md), which are
benches that run.

## 1. A link in six lines

```verilog
kts_tx #(.W(288), .VC(2), .CMAX(64)) u_tx (/* offers in, forward wire out, backward wire in */);
kts_pipe #(.W(288), .VCW(1), .N(4)) u_wire (/* the forward wire and the backward wire, four stages each */);
kts_rx #(.W(288), .VC(2), .D(32)) u_rx (/* forward wire in, heads out, backward wire out */);
```

The sender offers one flit per VC (`req_valid`, `req_last`, `req_flit`) and
advances when `req_take` is high. The receiver presents one head per VC
(`out_valid`, `out_last`, `out_flit`) and the consumer pops it with `out_pop`.
Nothing else is wired, and nothing waits on the wire.

## 2. Sizing

**Depth `D` is the round trip.** Count the sender cycles from a flit leaving
`kts_tx` to its credit arriving back:

    RTT = 1 (tx register) + N_forward + 1 (rx landing) + 1 (FIFO) + 1 (credit register)
        + CRD_BATCH − 1 (worst batching wait) + N_backward

Full rate on one VC needs `D ≥ RTT`; two VCs sharing a wire need `VC × D ≥
RTT` to keep the *wire* full. The measured link
([measurements.md](measurements.md) §3) does exactly what the formula says:
1.000 flits per cycle at 0 and 4 stages with `D = 16`, and `2 × 16 / 71 =
0.45` bound, 0.42 measured, at 32 stages. Doubling `D` is the answer to a long
wire; nothing else changes.

**Where `D` lives.** Up to 64 deep per VC, LUTRAM is right and is what ships.
A 128-deep VC in block RAM costs 9 RAMB36 and *fewer* LUTs than a 32-deep one
in LUTRAM (189 against 446 at W = 288) — so a link that must cover a long
round trip is cheaper in LUTs, not dearer, when its buffer moves to block RAM.
Set `MEM = "block"` on the receiving end.

**Width `W`.** Any width; the header needs 64 bits and the AXI4 bridges need
`W ≥ 128` (a 40-bit address) and `W ≥ 9/8 × DATA_W`. Cost is linear in `W`
for the ends and the stages ([measurements.md](measurements.md) §2.1); the
switch's return muxes are `K:1` selects of `W` bits per output per VC and grow
with `K × VC × W`.

**Credits per pulse `CN_W`.** Four bits (15 per pulse) returns 32 credits in
three pulses; raise it only if the backward wire's occupancy matters.

## 3. Virtual channels

One VC per dependency class. A request and the response it will cause must
never share a VC — a full response buffer would then block the request that
would drain it. Two VCs cover a request/response protocol; a ring needs a
dateline VC on top. Everything on a VC is in order; nothing across VCs is.

## 4. Reset and bring-up

1. Hold `rst` on both ends and every carrier for the same window; the
   receiving end's FIFOs come out of reset several cycles after `rst` drops.
2. Do nothing. The receiver issues `D` credits per VC by itself once its
   buffers are ready; the sender's counters go from 0 to `D` and the link is
   up. A sender does not need to know `D`, and two ends configured with
   different `D` still interoperate — the receiver's is what counts.
3. Read `kts_tx.credits` if you want to see it happen.

## 5. Tunnelling through what is already there

A surface can be carried by any of three things a design usually already has
([examples.md](examples.md) §2–§4):

| you have | use | what it looks like |
|---|---|---|
| an AXI4-Stream path — register slices, a clock converter, a switch | `kts_over_axis` at each end | flits on one stream with `tuser = {vc, last}`, credit counts on a second; the path's `tready` never reaches the surface |
| an AXI4 interconnect | `kts_over_axi4` at each end | each end is an AXI4 master that writes flits and credits into the other end's two windows, and an AXI4 slave that owns two windows; single-beat posted writes, `B` discarded |
| a word stream — a transceiver's user interface, a cable, a model | `kts_over_serial` at each end | framed words with a checksum; `RELIABLE = 1` adds sequence numbers, retention and replay for a carrier that loses words |

In every case the surface that comes out is the one that went in, credits
intact. What the middle adds is round trip, and §2 says what to do about that.

## 6. Carrying a protocol over a surface

The other direction: an AXI4 or AXI4-Stream endpoint that should reach across
a surface without knowing it exists.

- **AXI4-Stream**: `kts_axis_in` on the source side turns beats into flits
  (`tdest` picks the VC), `kts_axis_out` on the sink side turns them back.
  `tready` on the source side is the credit; the sink side pops on `tready`.
- **AXI4**: `kts_axi4_in` is an AXI4 slave that packs a write (AW + W beats)
  into one packet and a read (AR) into one header; `kts_axi4_out` is the AXI4
  master at the far end. The far end puts the packet's tag on its AXI ID, so
  the responses name themselves. Requests go on `VC_REQ`, responses on
  `VC_RSP`. The slave behind `kts_axi4_out` must not interleave read data of
  different IDs.

## 7. Adding a carrier

A carrier is a module with a surface in and a surface out and nothing waiting
on the wire. The two invariants to keep:

1. **Everything you buffer is bounded by credits.** No more than `VC × D`
   flits are ever in flight toward a receiver, and every credit pulse stands
   for at least one flit that was in flight — so a FIFO of `VC × D` on either
   path cannot overflow. `kts_cdc` and `kts_over_*` are all this argument.
2. **No credit reaches the sender before your forward path is listening.**
   A carrier whose forward FIFO is still in reset while credits arrive would
   see flits it cannot take; gate the backward output on the forward side's
   readiness, as `kts_cdc` does.

Credits can be merged — they are counts — so a carrier may accumulate them per
VC and forward them at its own pace. Flits cannot.

## 8. Adding a protocol

Define the packet on top of the header (`kts_pkt.vh`): a `kind` from 7 to 15,
the `user` bits, the payload's byte length in `len`. Send a header then payload
flits with `last` on the final one, on one VC. Route by `dst` through
`kts_switch` ranges. Keep requests and responses on different VCs. That is all
a protocol needs from the transport; the AXI4 bridge is the worked example.

## 9. Reading a credit count

`kts_tx.credits` is the sender's view. If it is:

- **zero and staying zero**: the receiver's credits never arrived — a backward
  wire is not connected, a carrier's backward path is not out of reset, or the
  receiver's consumer never pops (credits are issued on pop).
- **more than the receiver's `D`**: a credit pulse was duplicated — a carrier
  registered one pulse twice, or a serial frame was replayed without a
  sequence check.
- **oscillating between `D` and `D − k`** while the wire is busy: the link is
  round-trip bound at `k` flits in flight; raise `D` (§2).

The receiver's assertion `buffer full on arrival` in simulation means a flit
arrived without a credit behind it: a sender with the wrong initial state, or a
carrier that duplicated a flit.
