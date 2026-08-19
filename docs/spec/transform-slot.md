---
title: The memory-port transform slot
summary: The interface a read-path transform module must present, extracted from the shipping port and quantiser; closes the "swappable with no declared interface" gap named in integrate/what-you-own.md.
tags:
  - spec
  - mas
  - transform
---

# The memory-port transform slot

The read path between DRAM beats and response flits has a per-request
transform stage (`integrate/what-you-own.md` §2). This page is the interface a
replacement must present. The shipping example is KohakuTPU's FP16→MXFP7
quantiser; the template is `src/templates/transform/`.

## Position and selection

- **One instance per memory port.** Every consumer behind a port contends for
  it; a mesh buys transform throughput with more ports, which makes it a
  topology decision, not a module parameter.
- **Selected per request** by descriptor flag bit [4] (`TRANSFORM`, called
  `QUANT` in the TPU). The same port serves transformed and untransformed
  fetches in one program.
- The host upload path carries its **own instance** of the same slot on the
  write side, so uploading and fetching never contend.

## The ports

| port | dir | width | contract |
|---|---|---|---|
| `clk`, `rst` | in | 1 | the port's clock; `rst` active-high, used synchronously — same domain as the port, never a foreign one |
| `start` | in | 1 | one-cycle pulse opening an entry; configuration is valid during `start` |
| `b_layout` | in | 1 | transform configuration captured at `start` (the TPU's A/B packing select; a replacement defines its own meaning, and more config bits ride the request flags the same way) |
| `beat` | in | DATA_W | one source beat, **already registered by the port** |
| `beat_valid` | in | 1 | qualifies `beat`; beats are **pushed at line rate**, never handshaken |
| `need_beat` | out | 1 | reserved for a transform that cannot take line rate; the port ignores it today, so a compliant module ties it high or drives it truthfully |
| `done` | out | 1 | one-cycle pulse: the entry's outputs are final |
| `word0..word3` | out | DATA_W each | the transformed entry. Must be **stable from `done` until the next `start`** — the port latches them into its emit buffer |

## The three hard rules

1. **Fixed output shape.** A transformed fetch yields exactly four operand
   words per entry, whatever the source length. The emitter, the L1 fill
   protocol and the words-per-entry field all assume it; only a
   NON-transforming fetch may choose its own words-per-entry.
2. **The whole entry may be needed before anything can be emitted** (the
   quantiser's block scale is shared along K). The port is built for that:
   it does not expect streaming output, and `done` may come any number of
   cycles after the last beat.
3. **Input is push-only.** The port drives `beat_valid` from its own read
   state machine; a transform that needs backpressure must buffer internally
   (or drive `need_beat` and wait for a port revision that honours it).

## Timing conventions, measured not stylistic

- The port registers the beat before the transform (`mag_mem_port.v`: the
  read FIFO's BRAM output into the quantiser's DSP control was 9 LUT levels,
  4.399 ns, and set the WNS on every SLR1 probe until registered). A
  replacement gets a registered input and should keep its first level shallow.
- Entry latency is throughput-hidden: the port double-buffers (`p_*`/`e_*`)
  so the next entry's AXI read starts while the previous emits. A transform
  adding N cycles of latency costs N once per entry, not per beat.
- Datapath registers follow the no-reset convention
  (`EXTRACT_RESET = "no"` where a reset would only route as one).

## Source geometry

The transform declares its source entry size to the port as an AXI burst
length (the quantiser: 2048-bit entries = 8 beats at 256; output 1024 bits =
the 4 words). A replacement's geometry is its own, but the output rule (rule
1) still holds.
