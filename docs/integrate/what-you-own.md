---
title: Fixed, addon, convention, yours
summary: Four categories of thing in this framework, what may be changed in each, and which conventions are forced by the memory agent rather than merely advisable.
tags:
  - integrate
  - overview
---

# Fixed, addon, convention, yours

Most frameworks offer two categories: what they give you and what you write. This
one has four, and the two in the middle are where the useful decisions live.

| | what it is | may you change it |
|---|---|---|
| **Fixed protocol** | flit format, port handshake, memory request/response encoding, credit and retry, cross-mesh encapsulation | **No.** Change it and you are no longer on the framework |
| **Customisable addon** | ships working, and is *designed* to be swapped or extended: the transform stage in the memory agent, staging inside it, the adapter in a NoC endpoint's link | **Yes.** That is what the slot is for |
| **Convention** | how to design a well-behaved unit, with worked examples. Fill-and-tag, unit-to-unit messaging, how to spend instruction bits | **Follow or don't** — but know which ones are forced by the memory agent's design and which are genuinely free |
| **Yours** | the datapath, its memory structure, instruction semantics, pipeline depth | **Entirely** |

The rest of this page is each row in turn.

---

## 1. Fixed protocol

These are contracts. A unit that meets them attaches to any mesh the framework
generates and is discovered by any driver built on it; a unit that misses one
usually passes simulation and then hangs or corrupts on silicon, because most of
these protect against a lost or duplicated flit rather than against a wrong
value.

| | where it is specified |
|---|---|
| the flit: header fields, message classes, payload layouts per class | [spec/flit-format.md](../spec/flit-format.md) |
| the port: six signals, hold-until-taken, backpressure rules | [spec/compute-unit-port.md](../spec/compute-unit-port.md) |
| memory requests, responses and descriptors | [spec/memory-protocol.md](../spec/memory-protocol.md) |
| the control registers: per-unit block and orchestrator map | [spec/control-registers.md](../spec/control-registers.md) |
| which instruction payload bits are reserved | [spec/instruction-encoding.md](../spec/instruction-encoding.md) |

Two of these deserve naming outright, because they are the ones people
accidentally redesign:

**Credit and retry.** Dispatch is credit-controlled and the link is
hold-until-taken with hop-by-hop retry, not a valid/ready handshake. Both halves
of each are required and neither is separately choosable.

**Cross-mesh encapsulation.** A transfer to another mesh is addressed to the
*local* memory agent and carries its real destination in header fields the
message class does not otherwise use — on every flit of a burst, not just the
first. The routers never learn other meshes exist. You do not get to invent a
different scheme; you get to set the fields.

---

## 2. Customisable addons

An addon is a slot the framework already fills with something working, built so
that replacing it is a supported operation rather than a fork. There are three,
and they are not equally mature.

### The transform stage in the memory port

The read path between DRAM beats and response flits has a stage in it, selected
per request by a descriptor flag. KohakuTPU plugs a quantiser in: FP16 in DRAM
becomes its narrow block-scaled format on the way out, so software never sees the
internal format and no operand is converted twice. A project with a different
number format writes a different module.

Three properties make this a real slot rather than a feature:

- **It is per request.** The same memory port serves transformed and
  untransformed fetches in the same program, because the flag rides on the
  descriptor.
- **There is one instance per memory port**, so a mesh buys more transform
  throughput by having more ports — which makes it a topology decision
  ([mesh-topology.md](mesh-topology.md) §1).
- **The host upload path has its own instance**, so uploading and fetching do not
  contend. That was not free: they shared one under a mutex, and the mutex was
  the only reason an upload had to wait for a fetch.

The one thing the slot fixes is its output shape: whatever the source length, a
transformed fetch yields a fixed number of operand words per entry. A
non-transforming fetch may name its own words-per-entry instead.

> `src/kohakumas/mx_quant.v` is KohakuTPU's plug-in sitting in a framework
> package. It is a project's module in the framework's directory, and it should
> move.

### Staging inside the memory agent

A reserved address range backed by on-chip memory, so a working set that is
re-read across passes does not go back to DRAM each time. **Design stage, not
built.** The reason it is in this category rather than in "yours" is that the
address range and the request path already exist to hang it on.

Worth knowing before you reach for it: the framework's answer to operand reuse
today is not a cache but **shared fetch** — one instruction names the set of units
consuming the same operand, the lowest-numbered one issues a single descriptor,
and the memory agent delivers to all of them. That is the broadcast a shared cache
would exist to provide, done with compiler knowledge and without arbitration or
coherence. Any staging or caching proposal has to say what it adds beyond that.

### The adapter in a NoC endpoint's link

A module that sits between a router's local port and the endpoint on it,
presenting the same six signals on both faces, so that a pass-through
configuration is a straight wire. Anything that wants to observe or intercept an
endpoint's traffic — staging, tracing, an address remap — goes here without
touching the router or the unit.

**Proof of concept, not production**: it exists under `src/synth_top/poc/`. The
interface shape is the durable part.

---

## 3. Conventions

A convention is neither a spec nor a default implementation. It is *here is how we
did it, here is why, and here is what breaks if you deviate*.

The important distinction, and the reason this row exists at all: some of these
are **forced** — the memory agent hands you data in a particular shape whatever
you do, so a unit that ignores the convention is not being unconventional, it is
being wrong. Others are **free**, and the two production units genuinely differ
on several.

### Forced by the memory agent

| convention | what breaks otherwise |
|---|---|
| **Tag your request; let the response name its own placement.** The transaction id you send is echoed on every response, so a response says which slot it belongs in. | Responses interleave with other traffic and may complete in an order you did not choose. A receiver with a cursor of its own places data in the wrong slot, silently. |
| **Demux inbound flits by type, never by arrival position.** | Another sender's flit lands between your descriptor and its data. Framing by position splices two streams into one plausible wrong result. |
| **Dispose of write acknowledgements.** Nothing consumes them. | Held, they sit at the head of your receive queue, raise your busy line permanently, and wedge the instruction stream behind them. |
| **Accept and drop flit types you do not understand**, ideally with a simulation-only message naming the type. | Same wedge. Silent loss is the hazard, which is why the message matters. |
| **One descriptor per run, and per write burst.** The agent walks the address sequence itself. | A requester that issues one request per entry pays a memory round trip per entry — a latency where the protocol offers a throughput. On the write side, one transaction per word saturates the agent's transaction rate long before its bandwidth. |
| **A transformed fetch returns a fixed number of words per entry.** | Your entry assembly has to match, or you are re-packing what the transform already packed. |

### Free — and the two real units differ

Everything about the memory *inside* your unit. This is worth being explicit
about, because "the framework provides local memory" would be false:

| | KohakuTPU's matmul cluster | KohakuTPU's vector core |
|---|---|---|
| operand memory width | 928 bits | 256 bits |
| how many | two (one per operand), plus a resident accumulator tile per node, plus a register file | one, plus an instruction memory |
| primitives | block RAM for the operand memories, ultra RAM for the accumulator tile, distributed for command queues | block or ultra for the operand memory, distributed for the instruction memory |
| read latency | 1 for the operand memories, 2 for the accumulator tile | 1 or 2, following the primitive — ultra cannot do 1 |
| entry assembly | four response words are permuted into one 928-bit entry and committed on the last | a response word is stored as it arrives |
| who fetches | the unit's instruction sequencer, because a fetch is an instruction | the datapath, because it runs a program that decides |

Two units, one project, sharing none of it. If a framework had fixed any of these,
one of these units could not exist.

### Free, but there is a right answer

These are conventions in the strict sense: not forced, and you will regret
deviating.

**Assemble wide entries with one register, and assert the assumption.** One
assembly register is sufficient *only* because a single server delivers an entry's
words consecutively. That is a property of the server, not of the protocol — a
second server, a reordering fetch engine, or two senders into one unit would
interleave two entries into one and produce a plausible wrong result. Check it in
simulation at the point of assembly, so the message names the module.

**Make your operand memory addressable, not ping-pong.** An instruction that
retires on issue lets the next fill land while the current computation reads —
but only if the instruction can say *where*. Hardware double-buffering gives you
two regions and no way to leave a third operand resident, and no way to express a
reduction longer than the memory.

**Range-check a stream descriptor, and still count the stream out.** An offset
field is wider than the buffer it indexes, so an over-range burst wraps and
overwrites. Reject the writes — but keep counting the flits, or the next data
flit is read as a descriptor and the damage spreads.

**Acknowledge unit-to-unit transfers when asked, and let the acknowledgement
destination be redirectable.** The framework signals completion for
*instructions*; a transfer is not an instruction, so without an explicit
acknowledgement a sender that waits will wait forever. Redirectable because an
acknowledgement that goes back to the *sender* is useless when the sender is
another unit — nothing there consumes it, and the host cannot sequence a reader
behind a writer.

**Elect roles from the encoding, not by negotiation.** Where several units share
one fetch, hand every participant the same set and let each compare its own
coordinate against it. No negotiation, no extra bit, and no way for a compiler to
get it right for one member and wrong for another.

**Name memory primitives; never infer them.** Inference makes both the resource
cost *and the read latency* depend on a tool heuristic, and read latency sets
pipeline depth, which is a design decision rather than a synthesis outcome. Both
production units carry explicit read-latency parameters and comments explaining
what the number is load-bearing for.

**Retire at the point that makes the report true.** Ask what the host will do on
hearing the completion, and retire when that becomes safe.

**Spend the debug counter on something diagnostic.** It is the difference between
"it was slow" and "it was waiting".

---

## 4. Yours

The datapath. Its memories — how many, how wide, which primitive, what read
latency, how banked. Its pipeline depth. What its instructions mean. Whether it
runs macro-ops or programs. Whether it talks to other units at all.

Nothing in the framework has an opinion about any of that, and §3's table is the
evidence: two units in one project agree on almost none of it and both are
first-class citizens of the same mesh.

What the framework removes is not the design work. It is the **connection**
problem — how to be a node, how to ask for memory, how to be dispatched to, how
to report completion, how to be found by a driver. That work is identical for
every accelerator anyone would build here, it is unglamorous, it is where the
silent failures live, and it is solved.

---

## 5. Open questions

- **Two of the three addon slots are not production.** The memory-agent staging
  is a design, and the endpoint adapter is a proof of concept under
  `src/synth_top/poc/`. Only the transform stage is load-bearing today.
- **The transform slot has no declared interface.** It is a module instantiated in
  the read path with a particular port list; nothing states what a replacement
  must present. Until it does, "swappable" means "swappable by someone who reads
  the memory port".
- **`src/kohakumas/mx_quant.v` is on the wrong side of the framework boundary**
  (§2).
- **Conventions are not checkable.** Everything in §3 is prose and worked example.
  The forced ones could plausibly be assertions shipped with the framework — a
  bindable checker module a unit instantiates in simulation — and none exists.
