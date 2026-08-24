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

### The transform slot in the memory agent

A stage between memory and memory, driven by the memory mover, selected by an
**id** in its descriptor. KohakuTPU plugs a quantiser in: FP16 becomes its narrow
block-scaled format, so software never sees the internal format and no operand is
converted twice. A project with different arithmetic writes a different module
and changes nothing else.

Three properties make this a real slot rather than a feature:

- **Selection is an id, not a bit.** `0` is bypass, `k` is occupant `k`, and the
  mode bits beside it are opaque — the framework carries them and never reads
  them. Nothing in framework code is named after a number format.
- **There is one instance per memory agent**, not one per port and not one per
  compute unit. A per-port instance could never be busy in parallel with another
  anyway: every port master converges onto one DRAM master.
- **It is off the fetch path.** A compute unit reads operands already in their
  final format. Conversion is paid once per tensor rather than once per read,
  which is what a hidden state re-read across passes needs.

The one thing the slot fixes is its output shape: a transformed entry yields a
fixed number of words whatever the source length, and the bypass occupant obeys
that too.

The cost of this arrangement is that a single-use operand needs an explicit
mover pass, which the compiler or the control processor has to schedule. DRAM
traffic is unchanged; latency is two passes rather than one.

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

It ships as a template with a bench of its own:
`src/templates/adapter/kh_endpoint_adapter_template.v`, checked by the
`adapter_template` bench — `STAGE=0` must be a straight wire, `STAGE=1` must
hold the flit until it is taken, and the observe taps must count exactly the
transfers. Nothing in the reference instance instantiates it, so the interface
shape is the load-bearing part, not the implementation.

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
| **An entry is `entry_words × DATA_W/8` bytes, and a fetch is never transformed.** | Requesting a conversion is not a thing a unit can do; operands reach it in final form. Assuming otherwise means re-packing what was already packed. |

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

### 4.1 The same split, on the software side

`kohakuaccel` is the framework and a project sits on top of it. The line is not
"generic utilities against specific ones" — it is **mechanism against
vocabulary**:

| | `kohakuaccel` (mechanism, every project) | a project (vocabulary, one machine) |
|---|---|---|
| L5 | tensor/buffer protocol, op registration | the op library |
| L4 | `dims`, `record`, `kernel`, `In`/`Out`, dim solving, `units`, `loop` | what `<<=`, `@` and `+=` **mean** |
| L3 | graph, values, bands, lifetimes | which ops exist |
| L2 | `Arena`, `Buffer`, the **`Layout` protocol**, placement | the layouts themselves |
| L1 | program-per-unit container | which instructions exist |
| L0 | `Field`/`InstFormat`, artifacts, dispatch, round packing, await accounting | the ISA field table |

The framework never knows what a layout *means*. It knows a layout can answer
`nbytes(shape)`, `pack(array)` and `unpack(raw, shape)`. That is the whole
contract, and it is why the same L2 serves a machine whose native order is 4x4
sub-tiles and one whose native order is Morton-ordered tiles.

**The generality test.** A ray tracer changes only the right-hand column. Nothing
below is a real API — `L.slab`, `L.trace` and `L.shade` are the statement kinds
*that project* would define, and they are the point:

```python
@kernel
def render(scene=In(NPRIM, 8), rays=In(NRAY, 8), img=Out(H, W), *, batch=64):
    with units(rays.tiles(batch)) as i:
        bvh = L.region(batch, like=scene)     # -> a "load nodes" instruction
        hit = L.slab(batch)                   # -> the resident hit record
        for d in loop(scene.depth):
            hit <<= L.trace(bvh, rays[i, d])  # -> a "trace" instruction
        img[i] <<= L.shade(hit)               # -> a "shade" instruction
```

`dims`, `units`, `loop`, `In`/`Out`, dim solving, the arena, the dispatcher and
the round/await rules are reused untouched. What the project supplies is the
statement kinds, their layouts, its ISA and its kernel library. The same holds
for a DSP mesh (`L.fft`, `L.window`) or a CPU mesh (`L.load`, `L.branch`).

The split is enforced rather than intended: `driver/tests/test_isolation.py`
walks every `kohakuaccel` module in a subprocess and fails if importing them
pulls in any project module — see [software-stack.md](software-stack.md) §6.

---

## 5. Open questions

- **Two of the three addon slots are not in the shipping image.** The
  memory-agent staging has RTL and a bench (`mag_stage`, `mag_stage_port`) but
  no ship instantiates it; the endpoint adapter is a template with a bench and
  no instantiation at all. Only the transform slot is load-bearing today.
- **The transform slot's geometry is declared per agent, not per occupant.**
  `IN_BITS`/`OUT_WORDS` are parameters on `mag_xform`, so a bank holding two
  occupants of different shapes cannot describe itself. With one real occupant
  this is correct; more than one needs the geometry indexed by id. The occupant
  registers already carry the shape per id — `0x04` reads back
  `{OUT_WORDS, IN_BITS}` for the selected occupant — so the reader can discover
  what the parameters cannot express.
- **Conventions are not checkable.** Everything in §3 is prose and worked example.
  The forced ones could plausibly be assertions shipped with the framework — a
  bindable checker module a unit instantiates in simulation — and none exists.
