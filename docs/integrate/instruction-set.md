---
title: Designing your instruction set
summary: Which bits you own, how the dispatch path delivers them, how completion and faults come back, and how to keep an encoding a compiler can emit.
tags:
  - integrate
  - isa
  - guide
---

# Designing your instruction set

The framework carries instructions to your unit and completions back. It has no
opinion about what an instruction *means* — that is the whole point. What it does
have is an envelope, a delivery mechanism with a shape, and a completion protocol
you have to fit into.

This page is about spending the bits well. The normative field allocation is
[spec/instruction-encoding.md](../spec/instruction-encoding.md); the flit layout
around it is [spec/flit-format.md](../spec/flit-format.md).

---

## 1. You are not designing an ISA from scratch

The instruction space is **shared**. Three parties own bits, and only the third
is yours:

| owner | what it defines | you |
|---|---|---|
| `kohakuaccel/noc` | the flit header: routing, message class, transaction id, batch marker | obey it |
| the system node | the memory instruction set: read and write descriptors, streaming fetches, layout and transform flags, the memory mover's commands | **use it** |
| you | the `CU_INST` payload: what your unit computes | define it |

The middle row is the one that changes how you should think about this page. The
memory agent already has an instruction set, and it is a good one — it expresses
"fetch this run of entries, transformed this way, and deliver each response
tagged with where it goes". **Those are instructions you will issue, not
instructions you have to invent.** Your ISA sits on top of a memory ISA that
already works.

Practically, that means: before designing an opcode, find out what the memory
protocol already does for you. Streaming fetches, per-request selection of the
read-path transform, multi-destination delivery of one fetch, burst writes with
one descriptor — all of these exist, and an opcode that reimplements one is an
opcode you will regret. Read
[spec/memory-protocol.md](../spec/memory-protocol.md) before this page's §5.

There is a fourth instruction set above all of them, and it is worth knowing
about even though you will not extend it: the host's **control program** — three
opcodes, write, poll and done, executed by a small engine so that a whole run is
one host transaction and the host is not in the loop per poll. That is the layer
your driver emits into, and it is [software-stack.md](software-stack.md) §3.

Each layer is narrower than the one above and none can express the one below.
That is deliberate: the control program cannot branch, so your compiler unrolls;
your unit cannot walk DRAM affinely, so it hands the memory agent a descriptor;
and the memory agent has no idea what the bytes mean, which is why the transform
stage is a slot rather than a feature
([what-you-own.md](what-you-own.md) §2).

---

## 2. The envelope

An instruction is **one flit**: a routing header, then a payload that is yours.

```
    ┌──────────────── header: the framework's ────────────────┬─── payload ───┐
    │ dst_x  dst_y  src_x  src_y  type  txn_id  last  rsvd    │  yours        │
    └─────────────────────────────────────────────────────────┴───────────────┘
```

| field | who sets it | what it does |
|---|---|---|
| `dst_x`, `dst_y` | **the dispatcher**, from `PROG_DST` | routes the flit. Overwritten in flight; whatever you staged there is discarded |
| `src_x`, `src_y` | **the dispatcher**, its own coordinates | where the completion goes. Your unit never has to know the orchestrator's address |
| `type` | you, and it must be `CU_INST` | how the receiving unit separates instructions from everything else |
| `txn_id` | **you** | an 8-bit program id. Reported back verbatim on a batch completion |
| `last` | **you** | marks the final instruction of a batch |
| `rsvd` | reserved on `CU_INST` | other message classes use it; do not |
| payload | **you**, all of it | your opcode and operands |

Two consequences worth internalising before you design anything:

**An instruction is one flit, so it is one payload wide.** There is no multi-flit
instruction. If your operation needs more state than fits, split it: send setup
instructions that load state into the unit, then an instruction that runs. That
is what KohakuTPU's vector core does — two of its three opcodes exist only to
load state.

**You do not choose the destination in the encoding.** The destination comes from
a control register, and one dispatch goes to one unit. Work for four units is
four dispatches. If you want an instruction that *fans out*, you encode the peer
set in the payload and let each unit work out its own role — see §5.

---

## 3. How the bits reach your unit

```
    host                    orchestrator                     mesh          unit
    ────                    ────────────                     ────          ────
    write payload words  ─► staging RAM
    write PROG_DST                                                    (destination)
    write PROG_BASE                                            (first staging slot)
    write PROG_LEN                                                  (how many flits)
    write PROG_CRED                                                   (credit seed)
    write PROG_KICK      ─► dispatcher
                            reads words, assembles a flit,
                            stamps dst from PROG_DST and
                            src from its own coordinates  ─►  routers  ─► inst FIFO
                                                                             │
                            NODE_STATUS  ◄─ CU_SIGNAL ◄─  routers  ◄─────────┘
                            SIG_DONE++
```

Three properties of this path constrain an ISA design:

**Dispatch is credit-controlled, and a completion is what returns a credit.** The
dispatcher will not push more instructions than the unit has published room for.
An instruction that executes without a completion permanently consumes one
credit, and the machine stalls later with nothing to point at. This is why
`exec_done` is not optional and not best-effort.

> A caution for the host side: a **batch** completion — the one produced by the
> instruction whose `last` bit is set — carries a different signal code and
> **does not refill a credit**. Credits are re-seeded per round by writing
> `PROG_CRED`, not accumulated indefinitely. See
> [software-stack.md](software-stack.md) §3.

**Order is preserved to one unit, and only to one unit.** Instructions arrive at
a unit in the order they were staged. Between two units there is no ordering at
all — different dispatches, different routes. Anything that has to happen after
something else on another unit is a host-side or compiler-side barrier, not an
encoding feature.

**The staging buffer is single-use while a dispatch runs.** The dispatcher
streams out of it as it goes, so refilling before the dispatch drains corrupts
the program in flight. Waiting for the *dispatch* to drain is not the same as
waiting for the unit to *finish executing* — the unit keeps working while the
next program is staged, and that overlap is where the concurrency comes from.

The control-register names above are
[spec/control-registers.md](../spec/control-registers.md).

---

## 4. Completion and faults

Your datapath drives three signals and the framework turns them into one signal
flit:

| you drive | the framework sends | `arg` carries |
|---|---|---|
| `exec_done`, `last` bit clear | `SIG_INST_COMPLETE` | your `exec_result` |
| `exec_done`, `last` bit set | `SIG_BATCH_COMPLETE` | the program id from `txn_id` |
| `exec_done` with `exec_fault` | `SIG_FAULT` | your `exec_result` |

A fault reports your result rather than the program id, on the grounds that *why*
it failed is more useful than *which batch* it was in — the host already knows
the batch from which node signalled. **If you need both, encode the batch into
your own `exec_result`.**

`exec_result` is 32 bits and is entirely yours. Real uses in the tree: a cycle
count for a program that ran, a fault code, a running count of instructions
retired by kind. Choose something that makes a wrong answer diagnosable, not
something that merely confirms success.

Faults are **per instruction**, not sticky. If a condition is detected
asynchronously — a malformed inbound stream, say, arriving between instructions —
latch it and report it once at the next instruction boundary, then clear.
`mx_cluster_cu` does exactly that, and its comment gives the reason: a malformed
burst should be one fault, not a unit that faults forever.

On the host side, signals do not queue: the orchestrator absorbs each one into a
per-node status word (with a counter, so a host polling slower than events arrive
can tell how many it missed) and increments a single global completion count.
Queueing them was tried and is a deadlock: unread signals fill the receive FIFO,
raise the orchestrator's busy line, and stop it accepting the very signals that
return credits.

---

## 5. Making the encoding compiler-friendly

Every rule below is a decision visible in the tree, with the failure it prevents.

**Fixed field positions. No variable-length encoding.** A decode is a wire slice
with no state:

```verilog
    wire [3:0]  i_op   = inst_flit[255 -: 4];
    wire [33:0] i_addr = inst_flit[251 -: 34];
```

Anything else costs a decoder and buys nothing — you are not fighting for
instruction cache space.

**Opcode at the top of the payload.** Both production units put it at
`[255:252]`. It makes the opcode readable at a glance in a hex dump of a staged
flit, which is a real debugging advantage when the only view you have is a
register window.

**Overlap fields by role, not by opcode.** Give one bit range one *role* — a
base address, a count — and let the opcode decide what it is the base of. Then
the hardware slices unconditionally and the encoder writes fields by name. In
KohakuTPU's cluster ISA, `addr` is the operand base for a fill and the
destination base for a drain, and `n` is entries in one case and sub-tiles in the
other; the decode wires do not branch.

**Zero must mean the behaviour that existed before the field did.** This is the
single most valuable property for an ISA that will grow. In the cluster ISA, a
drain sends to memory when its node-destination bit is zero, addresses the lower
bank when its bank bits are zero, and is local when its remote-mesh field is
zero — so every instruction word emitted before those fields existed still means
what it meant. Old machine code stays valid, old recorded traces stay
comparable, and a partially updated compiler produces the old machine rather than
an undefined one.

**Size the field for the worst case, not the common one.** A count field that
silently wraps produces a plausible wrong answer: in the cluster ISA the drain
count is 16 bits because the resident tile can hold more sub-tiles than eight
bits can express, and an 8-bit field would have silently re-drained the start of
the tile. Widen the field and move everything below it; do not overlay it on a
neighbour.

**One field, one fact.** Where a field is copied verbatim into something else —
a descriptor's flag byte, say — do not borrow spare bits of it for an unrelated
purpose. The cluster ISA gives the acknowledgement destination its own field for
exactly this reason.

**Decode derived decisions once, into a register.** If a control decision is an
arithmetic function of instruction fields, compute it at decode time and register
it. Computed combinationally, a multiply of two instruction fields feeding a
state machine's clock enable became the worst path in the unit.

**Do not encode what the unit can derive.** A shared fetch in the cluster ISA
names every unit in the sharing set, and each unit independently decides whether
it leads by comparing its own coordinate against the set. The compiler hands
every participant the same bits; nobody negotiates, and there is no "am I the
leader" bit that a compiler could get wrong for one member and right for another.

---

## 6. Worked example: KohakuTPU's cluster ISA

**This section describes one project built on the framework, not the framework.**
It is here because it exercises every rule above.

`src/kohakutpu/matmul/mx_cluster_cu.v` has three opcodes in the top nibble:

| op | means | principal fields |
|---|---|---|
| `FILL` | fetch operand entries into local memory | base address, count, which operand, destination offset, bank, peer set, pre-quantised flag |
| `GEMM` | sweep the loaded operands into the resident output tile | group counts, K blocks, accumulate flag, per-side offsets and banks, emit flag |
| `DRAIN` | write the resident tile out | destination base, sub-tile count, fused flag, node-or-memory, destination node, buffer id, flags, acknowledgement node, remote mesh |

The instructive parts:

- **`FILL` is a descriptor, not a loop.** One instruction becomes one request
  flit, and the memory agent returns the whole run. The unit places words as they
  arrive and counts entries completed; it has no address cursor.
- **`FILL` selects the read-path transform, per operand.** One bit says whether
  the operand in DRAM is already in the datapath's format or needs converting on
  the way out, so the two operands of one multiply may differ. This is §1's point
  made concrete: the transform is a project's module, and the *instruction bit
  that selects it* is a project's bit, but the mechanism carrying both is the
  framework's memory protocol. Keeping it a property of the operand carried by
  the instruction is what keeps the memory agent free of an address map.
- **`GEMM` retires on issue.** The sweep runs behind the sequencer, so holding
  the instruction would only stop the unit filling the other half of its operand
  memory — which is why the operand memory is addressable rather than
  double-buffered by hardware.
- **`DRAIN` retires when the last write has left the unit**, not when the
  arithmetic stops. Retiring earlier reports completion ahead of the memory
  traffic it stands for, which is harmless right up until a later step reads what
  an earlier one wrote.
- **`acc`, `emit` and `fuse` are one bit each and each removes a whole
  instruction.** `acc` lets one output tile span several instructions, so a
  reduction longer than local memory becomes expressible. `emit` streams results
  out during the sweep and `fuse` turns the following drain into a barrier that
  waits for them rather than a second pass. These are the encoding paying for
  itself: a bit in an existing instruction instead of a new opcode.

`src/kohakutpu/vector/vec_cu.v` is a deliberately different shape — the second
worked example, and the useful one to read if your unit runs *programs* rather
than macro-ops:

| op | means |
|---|---|
| `IMEM` | write one word of the unit's instruction memory |
| `DESC` | set one field of one descriptor |
| `RUN` | start at a program counter; retires when the kernel halts, reporting its cycle count |

The whole ISA is three opcodes because the interesting instruction set is the one
*inside* the unit, which the framework never sees. The framework-level ISA exists
only to stage it and kick it. If your unit is programmable, this is the shape:
**the framework's instruction is a launch, not an operation.**

---

## 7. Open questions

- **`src/kohakuaccel/noc/noc_pkt.vh` declares a `CU_INST` payload substructure**
  (`NOC_INST_LEN`, `NOC_INST_CLASS`, `NOC_INST_BODY`) that neither production
  unit uses — both spend the payload from bit 255 downward as they choose. The
  header file itself notes that it is included by nothing. Whether that
  substructure is a reserved allocation projects must respect, or dead
  declaration, is for [spec/instruction-encoding.md](../spec/instruction-encoding.md)
  to settle; until it does, treat the payload as opaque and check the spec before
  assuming bits are free.
- **`txn_id` is 8 bits and serves two purposes.** On `CU_INST` it is the program
  id reported by a batch completion; on other message classes units use it as a
  tag they chose. Nothing enforces that a program id is unique across in-flight
  batches to the same node.
- **There is no framework-side assembler, disassembler or encoding
  description.** Every project writes its own encoder in Python and its own
  decode in Verilog, and nothing checks that the two agree except a test that
  compares emitted bytes against a second implementation. A machine-readable
  field description, with both sides generated from it, is the obvious fix and
  does not exist. See [software-stack.md](software-stack.md) §6.
- **Multi-destination dispatch is a host-side loop.** One kick is one
  destination. Whether the dispatcher should learn a destination list, or whether
  fan-out belongs in the encoding as it does for shared fetches, is undecided.
