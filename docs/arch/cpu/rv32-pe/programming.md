---
title: RV32 PE programming guide
summary: The programmer's view — the memory map, the control words, the communication idioms as code, dispatch and completions, boot, and running a program.
tags:
  - architecture
  - cpu
  - rv32
  - software
---

# RV32 PE programming guide

A program on the [RV32 PE](README.md) is ordinary RV32 code: it is loaded into
the unit's instruction window from outside, kicked, runs to a halt, and its
halt word is reported back to whoever kicked it. Software sees ordinary RV32
addresses; the memory frontend owns the mapping onto the machine.

This page is the practical form of the [architecture](architecture.md)
contract: the tables, the encodings, and the idioms.

## The memory map

One decoder, deciding on the **top four address bits and nothing else**.

| Software address | Region | Cost |
|---|---|---|
| `0x0xxx_xxxx` | instruction window | **faults** from the data side |
| `0x1xxx_xxxx` | scratchpad | 1 cycle, always hits |
| `0x2xxx_xxxx` | local control | 1 cycle |
| `0x3xxx_xxxx` | peer windows | **store only**; a load faults |
| `0x4xxx_xxxx` | vector scratchpad | **store only, and only when the SIMD extension is built.** Without it the region is unmapped and a store faults too |
| `0x6xxx_xxxx` | dispatch | **store only**; a load faults |
| `0x8xxx_xxxx` and above | global DRAM | hit 1 cycle, miss a fill round trip |
| anything else | — | faults |

`0x8xxx_xxxx` upward is a 2 GB software window onto physical DRAM — the decode
is address bit 31 alone. The translation is `DRAM_BASE | addr[30:0]`, OR-ed and
never added, because the base's low 31 bits are zero by construction, so it
costs no logic. The high bits of `DRAM_BASE` carry the mesh and aperture.

## Local control

Word-addressed from `0x2000_0000`. Only `addr[7:2]` decodes, so the region
aliases every 256 bytes; name the words by their symbol.

| Offset | Name | Access | Meaning |
|---|---|---|---|
| `0x00` | `CTL_STATUS` | read | bit 0 `flush_busy`, bit 1 writes outstanding |
| `0x04` | `CTL_FLUSH` | **store** | write back every dirty line — blocking |
| `0x08` | `CTL_INVAL` | **store** | drop every line, dirty included — blocking, one cycle per line |
| `0x0C` | `CTL_CAUSE` | read | halt cause of the last halt |
| `0x10` | `CTL_COREID` | read | `{y, x}`, this PE's mesh coordinate |
| `0x14` | `CTL_ARG` | read | the word the kick carried |
| `0x18` | `CTL_CYCLE` | read | cycles since the kick |
| `0x1C` | `CTL_INSTRET` | read | instructions retired since the kick |
| `0x20` | `CTL_WROUT` | read | writes not yet acknowledged |
| `0x24` | `CTL_SIGST` | read | completion queue: bits `[7:0]` entries held, bit 8 **sticky overflow** |
| `0x28` | `CTL_SIGHD` | read | the head completion: bits `[7:0]` sender id, bits `[15:8]` code |
| `0x2C` | `CTL_SIGARG` | read | the head completion's argument word |
| `0x30` | `CTL_SIGPOP` | **store** | retire the head of the completion queue |

`CTL_FLUSH` and `CTL_INVAL` block by design — see
[ordering](architecture.md#ordering). The flush returns with every dirty line
*acknowledged*, which is why flush-then-doorbell is a two-instruction idiom
with no barrier machinery; the invalidate returns with no line left for a later
load to stale-hit.

Read `CTL_SIGST` before storing to `CTL_SIGPOP`. A pop on an empty queue is
ignored by the requestor, so it is a software bug rather than a hardware
hazard, but nothing will tell you about it.

## Peer windows

A store into `0x3xxx_xxxx` becomes a word push to another PE's window. The
address *is* the routing:

```
   31..28   0x3          the region
   27..24   destination x
   23..20   destination y
   19       window: 0 = that PE's scratchpad, 1 = its instruction window
   18..5    granule index -- which 32-byte granule of that window
    4..2    word within the granule
    1..0    byte within the word, via the store's own byte enables
```

Word 0 of granule 0 of the scratchpad of the PE at `(2,2)` is a store to
`0x3220_0000`. `sb` and `sh` work: the byte enables travel with the push and
the receiving window applies them.

**Reads of a peer window do not exist** — the model is push-only, and a load
here faults. A consumer reads its *own* scratchpad with an ordinary load: one
cycle, zero fabric traffic, and a push landing in the very word being polled
returns the pushed bytes
([microarchitecture](microarchitecture.md#the-write-both-ports-can-make-at-once)).

## Push and doorbell

The machine's communication idiom, resting on one architectural rule:
**program order is arrival order, per destination**
([ordering](architecture.md#ordering)).

```
    core A                                core B
    ------                                ------
    sw   payload -> B's window
    sw   payload -> B's window
    sw   DOORBELL -> B's window   <-- LAST
                                          lw  doorbell, local scratchpad
                                          bne doorbell, seen, poll
                                          lw  payload,  local scratchpad
```

The corollaries a program must respect:

- The doorbell is the **last** store. Everything it announces precedes it.
- The flag must be a **different word** from the payload it announces, or a
  poll cannot tell a half-written entry from a finished one.
- A ring beats a single slot: advance a producer index last, never rewrite an
  entry a consumer may already have passed, and no handshake back is needed.
- Ordering holds **per destination**. Pushes to two different PEs have no order
  between them.

For bulk data, go through DRAM or the memory mover rather than pushing word by
word; a peer push is one word per store
([performance](performance.md#communication)).

## Dispatch and completions

The PE can kick other compute units itself and collect their completions, so a
mesh can run a dependency graph with the host out of the loop. This is the same
`CU_INST` / `CU_SIGNAL` protocol the host uses, driven from software.

### Sending a kick

Three stores into `0x6xxx_xxxx`. The address carries the destination and the
flit's header fields; the stored word carries the payload:

```
   31..28   0x6          the region
   27..24   destination x
   23..20   destination y
   19..12   txn          a program id locally; the final {y,x} in the target
                         mesh when rsvd[2] marks the flit remote -- software
                         owns which it is
   11       last
   10..8    rsvd
    4..2    field:  0 = ARG   write the argument register
                    1 = PC    write the start-PC register
                    2 = OP    the doorbell: fires the CU_INST, op = data[7:0]
```

Order matters: **`ARG` and `PC` write requestor registers and always accept;
the `OP` store is the one that constructs and sends the flit, and the only one
of the three that can stall.** Write the argument and the PC first, the opcode
last.

```
    li   t0, 0x6330_0000        # dispatch at (3,3), txn 0, field ARG
    sw   a1, 0(t0)              #   argument word
    sw   a2, 4(t0)              #   start PC        (field PC)
    li   t1, 1
    sw   t1, 8(t0)              #   op = 1: kick    (field OP -- fires)
```

The `OP` store holds until the requestor accepts the flit, so the instruction
after it has already handed the kick to the fabric.

### Receiving completions

`CU_SIGNAL` flits addressed to this PE land in an **8-entry queue**. Software
drains it through the control region:

```
    poll:
    lw   t0, CTL_SIGST(zero)    # bits [7:0] count, bit 8 sticky overflow
    andi t1, t0, 0xff
    beqz t1, poll
    lw   t2, CTL_SIGHD(zero)    # [7:0] sender id, [15:8] code
    lw   t3, CTL_SIGARG(zero)   # the sender's halt word
    sw   zero, CTL_SIGPOP(zero) # retire the head
```

**Check bit 8.** The queue is bounded, and a completion that arrives when it is
full is dropped and the overflow bit latches. That is deliberate: a lost
completion has to be *detectable*, because a unit whose completion vanished is
otherwise indistinguishable from a unit that never finished. If you see the
overflow bit set, the program dispatched more concurrent work than it drained,
and the fix is in the program.

## DRAM hand-off between units

Peer windows carry control; data of any size goes through DRAM. Four steps, in
this order:

```
    writer:   ... stores ...
              sw x0, 0(CTL_FLUSH)     -- blocking: every dirty line acknowledged
              sw doorbell -> reader's window
    reader:   poll its own doorbell
              sw x0, 0(CTL_INVAL)     -- drop every line, so nothing is stale
              ... loads ...
```

Without the flush the data is still in the writer's cache; without the
invalidate the reader may answer from a line it filled before the writer ran.
Both controls exist for this sequence.

## Boot

A program image arrives as an ordinary `CU_DATA` burst into the instruction
window, arguments as another into the scratchpad, then the standard kick. Boot,
argument passing and inter-core messages are one mechanism, not three — there
is no loader to go wrong.

### buf_id allocation

A `buf_id` is the field in an inbound data flit that names which of a unit's
windows the bytes are for, and at what granularity.

| `buf_id` | Target | Granularity |
|---|---|---|
| 0 | scratchpad window | raw 32-byte granules |
| 1 | instruction window | raw 32-byte granules |
| 3 | **reserved to the framework** | rejected by this unit |
| 4 | scratchpad window | one 32-bit word, byte enabled |
| 5 | instruction window | one 32-bit word |

Anything else is rejected, counted out of the burst, and reported. A granule
descriptor's `offset` and `len` are both in **granules**, and `offset + len` is
range-checked against the named window — rejected, never wrapped. A granule is
written as eight 32-bit words, so the unit stops accepting for eight cycles per
data flit; that backpressure is bounded by the unit's own progress and never by
another inbound flit.

### The kick, and completion

`CU_INST` with `op = 1`, a start PC, and one argument word readable at
`CTL_ARG`. Any other opcode retires without running anything. The kick never
overtakes the image it announces, and the completion — `CU_SIGNAL`, carrying
the halt word, code `0x00` for `ECALL` and `0x04` for `EBREAK` or a fault —
asserts that every write the program issued is acknowledged in memory. Both
guarantees are contract:
[architecture](architecture.md#the-unit-protocol).

## Writing and running a program

The toolchain is self-contained: a Python assembler and an RV32 golden model
live beside the benches, so no external RISC-V toolchain is needed.

```
python tests/pe/tools/rv_run.py my_program.s    # one program, on the full-system bench
python tests/pe/tools/rv_run.py --gate 1        # the verification suite, level 1..4
```

The first form assembles the file, runs it through the golden model to learn
what the answer should be, and checks the hardware against that model on the
same simulation the suite uses — one PE, real routers, the real memory agent. A
program that halts by `ECALL` with a result in `a0` gets its halt word, DRAM
contents and cycle counts reported.

Conventions the tooling assumes: the program is at position 0 of the
instruction window; `sp` is yours to set; the argument word is at `CTL_ARG`,
not in a register; and the clean way to finish is `ECALL` with the result in
`a0`.

## What software cannot do here

- **No system calls.** `ECALL` terminates the program; there is nothing to call
  into.
- **No self-modifying code.** The instruction window is unreachable from the
  data side; a store there faults. Patching is done from *outside*, through a
  `buf_id` 5 write, while the unit is not running.
- **No divide.** `div` and `rem` fault. Divide by a constant strength-reduces
  to `mulhu`; a general divide is a library routine.
- **No floating point.** There is no float register file to name.
- **No interrupt or timer.** A program polls, or it halts and is kicked again.
- **No reads of a peer, a dispatch register, or the vector scratchpad.** Every
  one of those regions is push-only, and a load faults.
