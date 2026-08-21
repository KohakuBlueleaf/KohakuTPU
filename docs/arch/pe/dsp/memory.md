---
title: Vector memory
summary: The vector scratchpad's banks and its two faces, how a 256-bit row relates to the 32-bit words the scalar core and the NoC write, and the vector register file.
tags:
  - architecture
  - pe
  - dsp
  - memory
---

# Vector memory

The DSP PE has two register files and two scratchpads, and the split is by
**width**, not by what is stored. The scalar side is 32 bits wide because that
is what an RV32 load returns; the vector side is 256 bits wide because that is
one flit, one memory-agent entry, and one cache line. Neither can be the other
cheaply, and this page is why.

```
   +------------------------------------------------------------------+
   |                            the PE                                |
   |                                                                  |
   |   scalar core                        vector unit                 |
   |   x0..x31   32 bits                  v0..v7   256 bits           |
   |      |                                  |                        |
   |      | lw / sw                          | vld / vst              |
   |      v                                  v                        |
   |   +--------------+                 +----------------------+      |
   |   |  scratchpad  |                 |  vector scratchpad   |      |
   |   |  2048 x 32   |                 |  1024 x 256          |      |
   |   |  2 BRAM      |                 |  8 BRAM (SIMD banks) |      |
   |   +--------------+                 +----------------------+      |
   |      ^      ^                          ^        ^                |
   +------|------|--------------------------|--------|----------------+
          |      |                          |        |
       the NoC   +------ sw, store only ----+        the NoC
      (buf_id 0)                                    (buf_id 2)
```

## Why a 256-bit array is eight arrays

An FPGA block RAM is not a wide memory with a width knob. A `RAMB36E2` in
true-dual-port mode presents **36 bits per port**, so any array wider than that
is several tiles no matter how it is described. The choice is not "one wide
array or several narrow ones" — it is only whether the several are explicit.

They are explicit here, as `SIMD` banks of 32 bits:

```
   word index:   0    1    2    3    4    5    6    7    8    9   10  ...
                 |    |    |    |    |    |    |    |    |    |    |
   bank:         0    1    2    3    4    5    6    7    0    1    2
   row:          0    0    0    0    0    0    0    0    1    1    1

   bank b holds every word whose index mod SIMD is b, at row (index / SIMD)
```

Banking that way gives the array its two faces for free:

| Face | Port | Sees | Used by |
|---|---|---|---|
| **narrow** | A | one 32-bit word, byte-enabled, in one bank | the NoC window writer, alone |
| **wide** | B | one row: every bank, with per-bank byte enables | `vld`, `vst`, and the scalar core's `sw` |

The narrow face is what makes a NoC delivery ordinary: a peer or the mover
writes 32-bit words into this window exactly as it writes into the scalar one,
one word at a time, with no read-modify-write anywhere. The wide face is what
makes `vld` one cycle.

**Every tile is fully depth-utilised.** 1024 rows is a `RAMB36E2`'s natural
depth at the 1K × 36 aspect a 32-bit port selects, so the tile count is exactly
`SIMD` and scales with the datapath rather than with a capacity guess: 8 BRAM
at eight lanes, 2 at two.

## The scalar core can store here but cannot load

The vector scratchpad is a region of the ordinary address map, `0x4xxx_xxxx`,
and it is **store-only from the scalar side** — exactly like a peer window. A
scalar store stages data for the vector unit to read with `vld`; a scalar load
of this region faults.

The reason is the base core's critical path. The load-return path — array,
cross-port bypass, sub-word extract, forwarding network — is what sets the
scalar core's 410 MHz, and adding a fifth region to the load mux would cost
frequency on every load in every program, to serve an access a `vld` already
performs better. A fault is a better answer than a slow one, and the region is
unmapped entirely when the extension is not built.

The store direction is nearly free, and **which port it uses is a timing
decision, not a plumbing one.** A scalar store writes through the port the
*vector unit* owns — the wide one — with the byte enables of a single bank,
because only one instruction is in MEM at a time and that port is therefore
free whenever no vector instruction is using it.

The alternative, sharing the NoC's port and arbitrating, is what the design
does not do. Arbitration would need the NoC's write enable, which is
combinational from the receive FIFO's empty flag, and that signal would then
reach the MEM stage's stall, the fetch hold, and the instruction window's
address — a path measured at **93.6 MHz of the assembled PE's clock**. The
framework's own requestor registers its push handshake for exactly this reason;
a window write is the same trap one level down.

The cost of the choice is one interlock: a vector load one instruction behind a
scalar store into this window takes a **bubble in decode** — the same mechanism
the base core's load-use hazard uses. It is a bubble rather than a stall
deliberately, since the vector unit's stall holds the MEM stage, and a stall
that waits for something *in* the MEM stage waits forever.

## No cross-port bypass here, and that is deliberate

The scalar scratchpad carries a byte-wise write-through between its ports: when
the NoC writes the very word the core is reading, the core sees the new bytes.
It costs 38 LUT and sits on the critical path, and it is bought because **the
collision is the doorbell** — a peer pushes exactly the word a poll loop is
reading, every time.

The vector scratchpad carries no such bypass, because the case is the opposite.
Doorbells still live in the scalar scratchpad; this array carries bulk data,
and bulk data is ordered by push-and-doorbell: the payload is written *before*
the doorbell the consumer is waiting on. A program that reads a row while the
NoC is writing it has violated the protocol rather than used it. The array
therefore keeps the permanent assertion that says so, at zero LUT.

## Alignment is a contract

`vld` and `vst` take an ordinary `rs1 + imm` address and require it to be a
multiple of the vector width — 32 bytes at eight lanes. A misaligned vector
address **faults**, in the same stage and by the same path as any other bad
address.

That is what lets the wide face be a plain row index with no rotate, no
merging, and no second read. It also shapes kernels: a stencil cannot reach
"one element earlier" with a misaligned load, so it loads two aligned vectors
and slides them past each other with `vsldw` ([lanes](lanes.md#when-lanes-must-talk)).

## Getting data in

| Path | How |
|---|---|
| from another unit or the host | a `CU_DATA` burst with `buf_id` 2 (granules) or 6 (one word), the same mechanism that fills the scalar window |
| from the scalar core | ordinary `sw` into `0x4xxx_xxxx` |
| from DRAM | scalar loads through the internal L1, then `sw` — or a mover delivery straight into the window |

The window writer treats this array as a third target beside the instruction
window and the scalar scratchpad, so nothing about program load, argument
passing, or peer delivery changes because a PE has a vector unit.

## The vector register file

Eight registers of 256 bits, two read ports, one write port, read latency 1.

Two read ports means **two mirrored arrays written in lockstep**, because no
primitive offers two independent read ports — the same construction the scalar
register file uses, for the same reason. Read latency 1 is pipeline structure,
not a cost: the addresses are captured at the end of EX so the data is out in
MEM, exactly where the scalar file's operands arrive relative to its own
stages.

**There is no write-through bypass**, and this is the one place the vector file
differs from the scalar one. The scalar file needs one because a write lands at
the same edge that captures a read four instructions behind it. Here the same
hazard is handled a stage earlier by a stall, because a bypass mux at 256 bits
is 256 LUT on the widest path in the unit against 32 for the scalar one —
[pipeline](pipeline.md#hazards).

### Why eight registers, and why the count is nearly free

A distributed-RAM primitive is 32 entries deep. A file of 8 leaves 24 of every
32 unused while costing the same LUTs as 32 would, which is why widening the
file is nearly free and narrowing it saves nearly nothing —
[performance](performance.md) has the measurement, and it is the mirror image
of the scalar core's register-file result, where a 32-entry file in a
1024-deep block RAM was 3.1 % depth-utilised and the LUTRAM form shipped
instead.

Register numbers are **immediates in the instruction**, so the count is a
parameter of the build and never of the encoding: the encoding always allows
32, and a build that carries eight faults on `v8` rather than aliasing it.
</content>
