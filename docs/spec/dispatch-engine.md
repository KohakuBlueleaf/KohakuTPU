---
title: Dispatch engine
summary: The hardware queue beside a node's processor that issues its control-register writes at one a cycle, each WAIT held on a completion counter the engine keeps itself.
tags:
  - spec
  - normative
  - sysnode
---

# Dispatch engine

> **Kind: Fixed** for the entry format, the codes, the window and the
> interlocks. Depth and counter widths are parameters.

Reference: `src/kohakuaccel/sysnode/dispatch/dispatch_engine.v`, wired into
`src/kohakuaccel/pe/rv64-sys/rv64_syscore.v` (`DE_DEPTH`, 0 builds none).

A node dispatches by writing registers: a mailbox's `DST`, `ARG0`–`ARG3` and
`GO` for an instruction or a fetch request, a mover's walker registers and
`CTRL` for a move. Every dependency is "unit u has retired k instructions". The
engine takes both as **entries** in a queue and issues them while the processor
reads ahead; it counts every unit's completions in hardware, so no completion
reaches the processor and no wait blocks it. It never decodes what it writes.

## 1. Entries

An entry is a 6-bit code and a 64-bit value.

| Code | Entry |
|---|---|
| 0–31 | a write of target register `code` with the value |
| 32 | `WAIT`: value `{src[60:56], want[CTR_W-1:0]}` |

On the RV64 node the targets are the processor's own control registers: codes
0–7 the mailbox (`0x40 + 8c`), 8–23 the mover (`0x100 + 8(c-8)`), 24–31 the
interlink window (`0xC0 + 8(c-24)`). An engine write goes through the same
register decode as a processor store; a processor store to one of those windows
takes the cycle and the engine holds its write.

A `WAIT` holds the queue until its source's count has reached `want`: until
`(count - want) mod 2^CTR_W` is below `2^(CTR_W-1)`. A source below `NCTR` is a
unit counter; `NCTR` is the mover's done count since the last clear. A `WAIT`
therefore stays within `2^(CTR_W-1)` of its count, which a credit-bounded
dispatcher guarantees.

## 2. Issue

One entry leaves the head a cycle, in queue order. A write is held while:

- the controller writes a shared window that cycle;
- it is the mailbox commit (`GO_CODE`) and a flit is still offered, or the
  previous commit is under `GO_GAP` cycles old, so the mailbox has shown it;
- its code is `MV_LO` or above (mover, interlink) and a move the engine started
  has not finished: the mover keeps no queue, and a doorbell must follow the data.

A write of `MV_GO_CODE` with value bit `MV_GO_BIT` starts a move.

## 3. Counters

A `CU_SIGNAL` the mailbox accepts, from a source mapped to a counter and with a
retirement code (`INST_COMPLETE` 0x00, `BATCH_COMPLETE` 0x01 —
[compute-unit-port.md](compute-unit-port.md) §4), is **claimed**: counted and
not queued for the processor. A fault, an acknowledgement, a unit's own codes
and a signal from an unmapped source still reach the processor's completion
queue. The map is `2^(2·POS_WIDTH)` entries of LUT RAM indexed by the source's
coordinates; the counters are LUT RAM with one write and three reads.

## 4. Registers (RV64 node, control region)

| Offset | Access | Meaning |
|---|---|---|
| `0x200 + 8c` | W | queue a write of register `c` (c < 32) |
| `0x300` | W | queue a `WAIT` |
| `0x300` | R | `STAT`: `[63:48]` 0xDE01, `[47:40]` log2 depth, `[39:36]` log2 counters, `[35]` overflow, `[34]` clearing, `[33]` head blocked, `[32]` enabled, `[31:16]` entries issued, `[15:0]` entries queued |
| `0x308` | W | `CTL`: `[0]` enable, `[1]` clear (queue emptied, counters zeroed over `NCTR` cycles, mover count rebased) |
| `0x308` | R | `MOVES`: `[43:32]` moves started, `[11:0]` moves done since the clear |
| `0x310` | W | `MAP`: `[31]` valid, `[19:16]` counter, `[11:8]` y, `[3:0]` x |
| `0x380 + 8k` | R | counter k |

A node built without an engine reads 0 there; `STAT`'s magic is how firmware
tells. A write queued while the queue is full is dropped and sets the overflow
bit until the next clear: firmware reads `STAT` for room instead.

## 5. Firmware

`software/firmware/kohakuaccel/package/interp.c` runs a package through the engine when
it has one ([package-format.md](package-format.md) §4.8): it maps the package's
units to counters 0..n-1, clears and enables the engine, queues each step's
entries (or copies an `ENGINE` step's), and at the end waits for the queue to
drain. While it waits for room it pops whatever reaches the completion queue: a
fault fails the package, the engine is disabled, and the words each unit has not
retired are handed to the firmware's counters so the usual quiesce drains them.
No progress for the timeout fails it with `TIMEOUT`.

## 6. Cost

Vivado 2024.2 out-of-context synthesis, xcvu13p-2L, 3.333 ns, default
parameters (DEPTH 64, NCTR 16, CTR_W 12): the engine alone 300 LUT (164 logic,
136 LUT RAM), 145 FF, no block RAM, register-to-register WNS +1.006 ns.
`rv64_syscore` with it: 8,352 LUT and 7,771 FF against 7,919 and 7,620
without, WNS +0.302 ns against +0.355 ns, the worst path the same core path in
both.

## 7. Verification

`tests/sysnode/dispatch_engine_tb.v` (`vlt.py dispatch_engine`): randomised
entry streams against a reference model — write order and data, both
interlocks, waits against the model's counters, claims, counter read-back,
overflow, clear. Three negative builds must fail: `NEG_GO` hides the offered
flit, `NEG_MV` the running move, `NEG_WAIT` makes the engine count more than the
model.
