---
title: KTS reference implementations
summary: The worked examples that ship with the project — each a bench that runs — and what each one demonstrates about a surface.
tags:
  - kohakutransmit
  - kts
  - examples
---

# KTS reference implementations

Every example is a testbench under `tests/transmit/`, listed in
`scripts/py/xsim.py`, and passes in the standard check. Each is the smallest
design that shows one property; the module set is the same in all of them.

## 1. A link across a wire of any length — `kts_link_tb`

Three copies of one link, differing only in the number of register stages on
the wire (`kts_pipe` with `N` = 0, 4 and 32), two VCs each, `D` = 16, random
offers and random pops for 6,000 cycles, then a 4,000-cycle window with every
VC offering and popping every cycle.

Every flit arrives in order, none lost, none duplicated, on every copy — and
the window measures `min(1, VC·D / RTT)`: **1.000 flits/cycle at 0 and 4
stages, 0.420 at 32 stages** against a bound of 0.45. A 32-stage wire is a
long die crossing; nothing in the link changed to cross it.

## 2. A surface through an AXI4-Stream path — `kts_over_axis_tb`

`kts_over_axis` at both ends; between them four register-slice stages per
stream, each refusing at random, on both the flit stream and the credit
stream. The receiving end's `tready` on the inbound streams is checked to be
constant 1 — the path's backpressure stops at the tunnel end and never reaches
the surface. Order, content, drain.

## 3. A surface through an AXI4 interconnect — `kts_over_axi4_tb`

Two `kts_over_axi4` ends connected crosswise through an interconnect model:
AW and W each through three register stages with independent random ready, B
answered by the slave. Every flit is a posted single-beat write into the far
end's flit window; every credit count a write into its credit window. The
windows never see a stray address; order, content, drain. **This is the
demonstration of latency insensitivity**: the interconnect's latency is
absorbed by the credits and by nothing else.

## 4. A surface over a word stream that loses words — `kts_over_serial_tb`

Two pairs side by side. The first is `RELIABLE = 1` over a channel that
drops **one word in forty** in both directions, with random ready and a
register in the path; the second is `RELIABLE = 0` over the same channel with
the drops off, the control. The reliable pair delivers every flit exactly
once, in order, despite the drops — the bench fails if the channel dropped
nothing — through go-back-N replay: the sender retains sixteen frames,
replays from the oldest unacknowledged after 200 idle cycles or on the second
acknowledgement that brought no progress; the receiver takes only the expected
sequence and repeats its last acknowledgement on anything else. This is the
carrier a transceiver link uses; on a lossless carrier `RELIABLE = 0` removes
the ring and the sequence.

## 5. AXI4 across a surface — `kts_axi4_tb`

An AXI4 master model writes forty random INCR bursts through `kts_axi4_in`,
eight register stages each way, `kts_axi4_out` and an AXI4 memory model with
random ready on every channel, then reads them back and checks every beat,
every ID and every `rlast`. Several transactions are outstanding at once;
the far end's AXI ID is the packet's tag, so nothing is tracked twice.

## 6. AXI4-Stream across a surface — `kts_axis_tb`

A stream source with random packets and `tdest` selecting the VC, through
`kts_axis_in`, twelve stages each way, and `kts_axis_out` into a sink with
random `tready`. Content and order per `tdest`.

## 7. A clock crossing — `kts_cdc_tb`

Two links at once: one whose receiver runs three times faster than its
sender, one three times slower. Order and content on both; the slow receiver
must deliver at its own clock — 0.9 flits per receiver cycle or more.

## 8. Width conversion — `kts_wconv_tb`

A 96-bit surface narrowed to 32 and widened back to 96 through two
converters in series, packets of random length with `last`. Content, per-VC
order, and every `last` at the same flit it left on.

## 9. A line of three ports — `kts_switch_tb`

A `kts_switch` with `K` = 3 as a line: port 0 owns destination 0, port 1
destination 1, port 2 everything above. Every input sends packets of a header
and 0–3 payload flits to random destinations on both VCs; every output checks
that each packet arrives whole, in order per (source, VC), with header fields
and payload `{src, dst, tag, index}` intact, and that every packet sent was
received somewhere.
