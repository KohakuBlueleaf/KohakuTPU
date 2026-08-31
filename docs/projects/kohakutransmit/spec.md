---
title: KTS specification
summary: The normative contract of a Kohaku Transmit Surface — the wire, the credit rules, the packet header, each carrier's guarantees, ordering and deadlock, and every parameter with its shipped value.
tags:
  - kohakutransmit
  - kts
  - spec
---

# KTS specification

Everything on this page is normative. A module that satisfies it is a surface
end, a carrier, a switch or a bridge; one that does not, is not. The
reasoning is in [README.md](README.md) and the practice in [manual.md](manual.md).

## 1. The surface

A surface is **one direction**. A link is two surfaces, one per direction,
independent of each other.

### 1.1 Forward wire (sender → receiver)

| signal | width | meaning |
|---|---|---|
| `valid` | 1 | a flit is on the wire this cycle |
| `vc` | `VCW` | the virtual channel it belongs to |
| `last` | 1 | the flit ends a packet |
| `flit` | `W` | the payload |

### 1.2 Backward wire (receiver → sender)

| signal | width | meaning |
|---|---|---|
| `crd_valid` | 1 | a credit count is on the wire |
| `crd_vc` | `VCW` | the VC it is for |
| `crd_n` | `CN_W` | how many credits, 1 … 2^CN_W − 1 |

### 1.3 Rules

1. **No `ready` on either wire.** Every signal is driven from a register and
   captured into a register. A carrier may insert any number of stages, any
   clock crossing, any serialisation between the two ends.
2. **A flit needs a credit.** The sender emits a flit on VC *v* only if its
   credit count for *v* is at least 1, and decrements it in that cycle.
3. **Credits are issued by the receiver.** Every credit the sender will ever
   hold comes over the backward wire: the receiver issues `D` per VC once its
   buffers are out of reset, then one for every flit its consumer removes. The
   sender starts at zero and does not need to know `D`.
4. **The receiver never drops.** Its buffer per VC holds `D` flits, and by
   rules 2 and 3 no more than `D` are ever in flight toward it.
5. **Credits may be batched.** A receiver may hold returned credits and send
   them as one count; it must send every credit it owes within a bounded time
   (`CRD_BATCH` and `TIMEOUT` below).
6. **Order.** The flits of one (surface, VC) arrive in the order they were
   emitted. Nothing is promised across VCs.
7. **Throughput.** For a round trip of `RTT` sender cycles (forward stages +
   receiver landing + buffer + credit batching + backward stages), a VC
   sustains `min(1, D / RTT)` flits per cycle; the wire sustains at most one
   flit per cycle across all VCs.

### 1.4 `kts_tx`

Offers per VC: `req_valid`, `req_last`, `req_flit`; `req_take` returns 1 in the
cycle the offer is emitted (a local handshake, not a wire). Round-robin among
the VCs that have both an offer and a credit. `credits` exposes the counters.

| parameter | meaning | ships |
|---|---|---|
| `W` | flit width | 288 |
| `VC` | virtual channels, 1 … 8 | 2 |
| `CMAX` | the most credits a VC can hold (counter width `log2(CMAX)+1`) | 64 |
| `CN_W` | width of a credit count | 4 |

### 1.5 `kts_rx`

A `kts_fifo` per VC; `out_valid / out_last / out_flit / out_pop` per VC to the
consumer; the credit return per rule 5.

| parameter | meaning | ships |
|---|---|---|
| `W`, `VC`, `CN_W` | as `kts_tx` | 288, 2, 4 |
| `D` | depth per VC = credits issued; power of two, ≥ 16 | 32 |
| `CRD_BATCH` | credits per return pulse | 4 |
| `TIMEOUT` | idle cycles after which a partial batch is returned; 0 = never | 16 |
| `MEM` | `"distributed"`, `"block"`, `"ultra"` | `"distributed"` |

A receiver issues no credit while any of its buffers is still in reset
(`xpm_fifo` holds `full` for several cycles after `rst` falls).

## 2. Carriers

A carrier takes a surface in and gives the same surface out. It may add
latency and clock domains; it may not drop, duplicate or reorder within a VC,
and it may not add a `ready` to the surface.

| module | in the middle | guarantees | parameters (ships) |
|---|---|---|---|
| `kts_pipe` | `N` register stages on both wires | zero logic; `N = 0` is wires | `N` (2) |
| `kts_cdc` | a dual-clock FIFO each way; credits accumulated per VC on the far side and pushed as counts | nothing overflows: forward depth ≥ flits in flight (`VC × D`), credit depth the same; no credit reaches the sender before the forward FIFO is listening | `D`, `VC`, `DEPTH` (`VC·D`, ≥ 16), `MEM` |
| `kts_wconv` | a receiving end at `WI`, a per-VC shift, a sending end at `WO` | a wide flit becomes ⌈WI/WO⌉ narrow flits with `last` on the final one; narrow flits gather into a wide one, zero-padded when `last` comes early; the packet's byte count (§3) is what survives | `WI`, `WO`, `D`, `CMAX` |
| `kts_over_axis` | flits on one AXI4-Stream (`tuser = {vc, last}`), credit counts on a second, each direction | the stream's `tready` never reaches the surface: flits wait in a FIFO the credits cannot overflow, credits wait as counts; the inbound streams' `tready` is constant 1 | `D`, `VC`, `DEPTH`, `MEM` |
| `kts_over_axi4` | every flit a single-beat posted write to the far end's flit window, every credit count a write to its credit window; two windows on an AXI4 slave port turn the far end's writes back into the surface | the interconnect's latency is the round trip; the windows are write-only and accept every beat; `B` is discarded | `ADDR_W`, `DATA_W` (≥ W + VCW + 1), `ID_W`, `FLIT_AT`, `CRD_AT`, `MY_FLIT`, `MY_CRD` |
| `kts_over_serial` | frames on a word stream: a header word, ⌈W/CW⌉ payload words, an inverted-XOR trailer carried with the carrier's `last` | with `RELIABLE = 1`: frames carry an 8-bit sequence, the sender retains `WIN` of them and replays from the oldest unacknowledged after `TIMEOUT` idle cycles or on the second acknowledgement that brought no progress; the receiver takes only the frame it expects, acknowledges every frame it takes, and repeats the last acknowledgement on an out-of-sequence frame. With `RELIABLE = 0` frames are fire-and-forget on a lossless carrier | `CW` (64), `RELIABLE` (1), `WIN` (32), `TIMEOUT` (512) |

The serial header word (`CW ≥ 64`): `[7:0] 8'hA5`, `[9:8]` type (0 flit, 1
credit, 2 ack), `[10]` last, `[14:11]` vc, `[22:15]` sequence, `[26:23]` credit
count, `[34:27]` payload words. The word after a carrier `last` is a header; a
frame that loses a word ends at the next `last` with a bad sum and is dropped
whole; the receiver discards to the next `last` when it sees anything else.

## 3. Packets

A packet is a header flit and zero or more payload flits on one VC, contiguous,
`last` on the final flit. Header layout (`kts_pkt.vh`, `W ≥ 64`):

| bits | field | meaning |
|---|---|---|
| `[3:0]` | `kind` | 0 data · 1 read request · 2 write request · 3 read response · 4 write response · 5 credit and 6 ack (serial carrier only) · 7–15 user-defined |
| `[7:4]` | `vc` | a self-describing copy of the wire's VC |
| `[15:8]` | `dst` | routing target |
| `[23:16]` | `src` | the sender |
| `[39:24]` | `len` | **payload bytes** — flit counts change through a width conversion, byte counts do not |
| `[47:40]` | `tag` | the requester's transaction tag, returned in a response |
| `[W-1:48]` | `user` | protocol-specific |

### 3.1 `kts_switch`

`K` ports. A receiving end per input; per (input, VC) a two-entry head queue
holding each flit with the output its packet goes to; per (output, VC) a
packet-locked round-robin over the inputs; a sending end per output. The
output for a packet is the first `o` with `LO[o] ≤ dst ≤ HI[o]`, decided at
the header and held to `last`. A VC never changes inside a switch. The same
module is a line, a ring or a crossbar by its ranges.

| parameter | meaning | ships |
|---|---|---|
| `K` | ports | 3 |
| `LO`, `HI` | `K × 8` bits, the destination range of each output | port 0: 0, port 1: 1, port 2: 2 … 255 |
| `W`, `VC`, `D`, `CMAX`, `CN_W`, `MEM` | as the ends | |

### 3.2 Ordering and deadlock

- Flits of one (source, destination, VC) arrive in order; nothing else is
  promised.
- **Request and response never share a VC.** A ring additionally needs a
  dateline VC. The switch does not enforce either; the protocol on top must.

## 4. Bridges

| module | AXI side | packets | contract |
|---|---|---|---|
| `kts_axi4_in` | AXI4 **slave** | AW + W beats → one `WRREQ` packet on `VC_REQ`; AR → one `RDREQ` header; `RDRSP` / `WRRSP` packets on `VC_RSP` → R beats / B | the transaction holds a slot here and its index is the packet's `tag`, which the far end puts on its AXI ID; per-ID order holds when an ID's traffic goes to one destination |
| `kts_axi4_out` | AXI4 **master** | `WRREQ` → AW + W, `RDREQ` → AR, with `awid = arid = tag`; B → `WRRSP`, R beats → `RDRSP` | one request packet issued at a time; the slave must not interleave read data of different IDs |
| `kts_axis_in` | AXI4-Stream **slave** | one beat = one flit, `tlast` = `last`, `tdest` = VC (or `VC_FIXED`) | `tready` is the sending end's credit for that VC |
| `kts_axis_out` | AXI4-Stream **master** | the receiving end's heads, packet-locked round-robin over VCs, `tdest` = VC | `tready` pops the head |

Beat encoding: a write beat flit is `{strb, data}` and a read beat flit
`{resp, data}`, so `W ≥ 9 × DATA_W / 8`; the header's `user` field holds
`{addr, size, burst, len, id}`, so `W ≥ 48 + ADDR_W + 13 + ID_W`. With a 40-bit
address the bridges need `W ≥ 128`.

| parameter | meaning | ships |
|---|---|---|
| `ID_W`, `ADDR_W`, `DATA_W` | the AXI4 port | 4, 40, 256 |
| `NSLOT` | outstanding transactions per `kts_axi4_in` (≤ 2^ID_W of the far end) | 16 |
| `VC_REQ`, `VC_RSP` | the two VCs | 0, 1 |
| `DST`, `SRC` | header fields the bridge stamps | 0, 0 / 1 |

## 5. Primitives

| module | what | note |
|---|---|---|
| `kts_fifo` | `xpm_fifo_sync`, first-word-fall-through, depth a power of two ≥ 16 | `full` and `empty` include the macro's reset-busy |
| `kts_afifo` | `xpm_fifo_async`, the same shape, two clocks | write-side reset |
| `kts_ram` | `xpm_memory_sdpram`, one write port, one read port, `READ_LAT` chosen by the caller | the replay ring of the serial carrier |

Memories are named, never inferred: the primitive and the read latency are the
caller's decision.
