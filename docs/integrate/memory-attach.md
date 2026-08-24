---
title: Memory attach — how a unit reaches the machine
summary: The framework's memory model is an IO model, which a unit whose inner memory system we do not own cannot implement. This is the design record for the attach product that fixes it — a shared tagged L2 at the mesh endpoint, where memory ranges hold lines and communication ranges never do, with the reasoning, the three options it was chosen against, and what it does not solve.
tags:
  - integrate
  - memory
  - noc
---

# Memory attach

A design record, not a specification. Nothing here is built. It exists so the
argument survives the discussion that produced it, including the several places
the first version of it was wrong.

---

## 1. The problem: the framework hands out a protocol

The memory model that ships is an **IO model**. A unit is handed a protocol and
implements it: build `MEM_RD_REQ` / `MEM_WR_REQ` descriptors, allocate
transaction tags, match responses back to them, respect one outstanding write,
honour credits.

`src/kohakuaccel/pe/rv32/noc/rv_noc_req.v` is 289 lines of exactly that, and its
header states the job plainly:

> *"everything about the framework memory protocol that RV32 software must never
> see."*

That file is a success. Software above it issues `lw` and `sw`. But the
*hiding* happens **inside a unit we wrote**, which means every new unit either
copies it or writes it again — and a unit we did **not** write cannot do either.

### 1.1 The difference that decides everything: who owns the protocol

| | IO model | memory model |
|---|---|---|
| what the unit does | builds descriptors, tracks tags, matches responses, counts credits | issues a load or a store |
| what memory is | **a device it drives** | **a space it inhabits** |
| who implements the protocol | every unit author, once each | the framework, once |
| can a foreign unit join? | **no** | yes |

The last row is not a matter of degree. A mature core emits AXI reads and a line
fill. It will never emit a `MEM_RD_REQ` flit, and it cannot be taught to without
owning its RTL — which, if we owned it, would make it not a foreign core.

So this is an **integration-reach** question, not a performance question. It is
the difference between shipping an accelerator and shipping an accelerator
*framework*.

---

## 2. What already works — so the gap is not overstated

It is easy to overstate this gap, and the first draft of this document did. The
following all work today and none of them need the attach product.

**The station bus already carries the whole card address space.** An AXI master
reaches every mesh's `S_AXI_MEM` **and** every mesh's `S_AXI_CTRL` through the
segment map (`sb_nmu`'s per-segment base/mask/translate decode). Other meshes'
memory and other meshes' control registers are both addressable now. The
"unified memory, the CPU sees all of DRAM including the other meshes" case is a
map choice, and the mechanism for it exists.

**An AXI master can already dispatch compute units.** Not by any special path —
by exactly the route the host uses: write the program into the orchestrator's
staging RAM, set `PROG_*`, kick. Anything that reaches `S_AXI_CTRL` can drive
units. A foreign core is not locked out of *using* the machine.

**Data already crosses meshes.** The mover pushes over the interlink, `CU_DATA`
carries a peer drain, and matmul and vector results reach other meshes today.
Cross-mesh is a shipping capability, not a hole.

**Explicit L2 exists in two forms, both selectable, both in ship tops.**
`mag_stage` behind `mag_stage_port` in the system node, and `noc_l2_adapter` at a
mesh endpoint — chosen per endpoint by `gen_mesh.py --l2-mag / --l2-cu /
--l2-vec`, at 8 URAM per CU adapter and 64 per agent.

---

## 3. The gap, stated exactly

Given all of §2, what is actually missing is one thing:

> **Nothing turns a miss into a flit on behalf of a client whose inner memory
> system we do not own.**

The consequence is precise. A mature core or unit can be a **host** — an AXI
master that drives the machine by hand-building dispatches and register writes.
It cannot be a **peer** that reaches the machine as address space, because
reaching a compute unit or a staging aperture *as memory* requires speaking the
flit protocol, and speaking the flit protocol requires being a unit we wrote.

---

## 4. Which tier the product lives at, and why not L1

This is where the first version of this document was wrong, and the error is
worth recording because it is easy to repeat.

`rv_l1` + `rv_noc_req` is a tagged cache whose miss becomes a flit. It works, it
passes 116 checks in `rv_front`, and it is the exact mechanism this document
wants. **It is not a product**, because:

> A unit's innermost cache is not ours to design. A mature core or unit has its
> own L1, its own fill logic and often its own MMU. What it exposes is an
> external memory face; nothing behind that face can be changed.

An L1 attach only works where we own the L1 — which makes it an implementation
detail of *our* core, not something a third party can adopt. The attach product
has to sit **below whatever the client already has**, which is the L2 tier.

### 4.1 The system node needs no extra layer; a mesh node does

```
SYSTEM NODE — our control processor
    pipeline -> L1 (ours) -> requester -> MAG converged path -> stage / DRAM
    no extra tier: it already sits INSIDE the memory system, beside
    mag_stage_port and mag_dram_port

MESH NODE — any unit, ours or foreign
    [ its own L1 / MMU -- not ours, not changeable ]
        -> TAGGED L2                        <- THE PRODUCT, MISSING
        -> flits -> MAG -> stage / DRAM
```

The asymmetry is real and it is the reason the framework needs a block at all.
A processor inside the system node reaches memory by being next to it. A unit at
a mesh port reaches memory only through the fabric, and something has to hold
lines and translate.

---

## 5. Two address classes, and only one is tagged

This is what "everything is memory" means concretely: not that everything is
*cached*, but that everything is *addressed*.

| class | examples | behaviour |
|---|---|---|
| **memory** | DRAM, L2 staging | cacheable — tag check, line fill, eviction |
| **communication** | a CU's instruction window, a peer's buffer, a doorbell, a control register | **never tagged.** Always miss. Every access becomes a packet |

A store into a communication range **is** a flit. A load from one **is** a
request and its response. No line is ever allocated, so there is nothing to
flush, nothing to evict, and no coherence question for that half of the map.

That is what lets a client dispatch work, push data to a peer and ring a doorbell
with nothing but loads and stores, while its working set still gets lines.

### 5.1 Blocking, and what real CPUs do about it

The obvious worry is that a communication access is a NoC round trip, so
"everything is memory" makes the client stall.

**Every mainstream ISA already draws this line, through memory attributes per
region** — ARM `Device` vs `Normal` (with the nGnRnE / nGnRE / nGRE / GRE grading
of Gathering, Reordering and Early-acknowledgement), x86 `UC` and `WC` against
`WB` via MTRR/PAT, RISC-V PMAs separating I/O from main memory. The split in the
table above is the same split, which is a good sign for it.

**But the property that matters here is not blocking — it is non-speculation.**
Device-typed regions are never speculatively accessed, and that is a correctness
requirement rather than a tuning one: a speculatively issued load into a doorbell
range *rings the doorbell*. A packet that should never have existed is a bug no
amount of latency tolerance repairs.

Blocking itself is probably tolerable, for two reasons that should be checked
rather than assumed:

- a communication **load** is a request/response round trip, DRAM-latency-shaped,
  not obviously worse than the cache miss the client already copes with;
- a communication **store** can be posted — `MEM_WR_REQ` needs no response for
  correctness, which is why the mover's writes are posted today.

**The integration consequence is the actionable part:** the range map must be
*visible to the client*, so its page tables or PMA can mark communication ranges
Device / non-cacheable / non-speculative. A client that cannot be told which
ranges are which will speculate into them. If an attach cannot express that, it
should be said at integration time rather than discovered as a phantom packet.

---

## 6. The three ways to attach, and why only one is a design problem

| | what it is | design problem? |
|---|---|---|
| **1. AXI master → station bus** | ships today | **No.** The address map is the integrator's choice, not ours |
| **2. AXI master → NoC bridge** | AXI slave in, flits out, stateless | **No.** Wiring and logic. Many variants, needs-driven |
| **3. Tagged L2 → NoC** | holds lines, translates misses | **Yes. This is the product** |

**On (1).** The map belongs to whoever builds the machine. What *is* framework
work is the **contract and its checker**: the fabric carries no `LOCK`, no
`CACHE`, no `PROT` and no timeout; `sb_nmu` is INCR-only, bounded by `MAX_BURST`,
and reserves response credit before injecting. A foreign master breaks these
silently. `tests/axi/sb_axi_check.v` already exists and belongs in the
integration deliverable rather than in the test tree alone.

**On (2).** A bridge with no tags, no lines, no flush and no coherence. It is the
right answer for a client that already has its own cache hierarchy — putting our
tagged L2 under a core that has an L2 means two tag arrays in series and a
coherence question nobody needed. It is also the **fill path of (3)**, which is
the useful structural fact: (3) is (2) plus a tag array, not a separate project.

---

## 7. The product

### 7.1 Slave faces — configurable, because one shape does not fit

**(a) AXI4 slave.** The widest door. MicroBlaze V and classic MicroBlaze,
VexRiscv and NaxRiscv, Rocket, CVA6, and mature third-party units all attach with
no changes to themselves.

**(b) Native port.** The port to *merge into*, not bridge to — the same
relationship `noc_cu_base` already has with a compute unit: instantiate it inside
your unit and speak its handshake directly.

The framework's own PEs use (b). **Giving a SIMD or SIMT PE an AXI interface
would be pure waste** — it has no use for burst types, transaction IDs or
response ordering, and every one of those is area spent re-describing a handshake
it already has. A GPU-class PE is genuinely different from a tensor unit here:
its accesses are gathers and indexed reads decided at run time, so it wants tags
where a matmul wants explicit staging.

### 7.2 Multi-master

**The number of slave ports is a parameter.**

Modern CPU and accelerator designs share one L2 across several units. A shared L2
with a configurable port count is the difference between a one-off attach and a
general block — and it is also where the cost argument turns favourable (§9).

### 7.3 Master face

Flits, through `noc_cu_base`. **32-byte lines**: the mover's word, the flit
payload and MAG's internal beat are all 32 bytes, and any other line size
fragments the machine at one of those boundaries.

### 7.4 Where it sits, and why not the router

At a **mesh endpoint** — the slot `noc_l2_adapter` already occupies, with proven
port contract, placement and URAM budget.

Not in the router. Caching inside the router is the one option in the cache round
flagged as high risk and the only one that can deadlock, because it makes routing
depend on tag state. At the endpoint, routing is untouched and the deadlock
argument for XY routing is unaffected.

---

## 8. It coexists with the explicit adapter

`noc_l2_adapter` (explicit, address-placed) and this block can both be present in
one machine, and even at one endpoint. Which a given endpoint uses is a design
choice for whoever builds the machine, not a framework decision.

They are right for different clients, and the existing conclusion in
`docs/notes/cache/README.md` — *prefer explicit staging* — remains correct within
its scope:

| client | addresses known | fits |
|---|---|---|
| matmul, vector | compile time: the sweep walks addresses the compiler computed | explicit staging |
| CPU-class PE, GPU/SIMT, foreign core | run time | tagged |

A cache in front of a GEMM sweep spends tags rediscovering what was written down.
A GPU's gathers are the opposite case. This machine has both, so it wants both
mechanisms, selected per endpoint exactly as the staging options already are.

---

## 9. Cost is a net, not a charge

The block costs a tag array, line storage and its own control.

It also **removes**, from every unit behind it: flit construction, the
transaction tag table, response matching, write-ordering state and credit
accounting — `rv_noc_req`'s 289 lines, paid once per L2 instead of once per unit.
That is why §7.2 matters to the arithmetic and not only to the flexibility: with
N masters behind one L2, the per-unit saving is multiplied and the tag array is
not.

**Unmeasured.** No out-of-context run exists and nothing here is a figure. But
the sign is not obviously a cost, and an evaluation that counts only the tag
array is counting one side of it.

---

## 10. Coherence: none, and say so

Single writer per line, software ordering, explicit flush — the contract `rv_l1`
already has.

State it and enforce it rather than leaving it undefined. An undefined coherence
model produces exactly the failure this project keeps meeting: a plausible wrong
answer with no exception raised.

---

## 11. What this does not solve

- **Liveness.** A miss on a congested fabric stalls the client; a miss that never
  returns hangs it. The fabric carries no timeout. A timeout-to-fault path is a
  prerequisite, and it is the hardest problem in this document.
- **It does not make a foreign core coherent** with anything, and does not try.
- **It does not replace the station bus.** A client that only wants DRAM and
  control registers should keep using it — that path is simpler and it works.
- **It is not a performance argument.** If the reason for wanting it is
  bandwidth, the explicit staging that already ships is the better answer.

---

## 12. Relationship to the rest of the machine

**The system-node control processor needs none of this.** It sits inside the
memory system already; a segment file is enough to let a 32-bit core name the
40-bit space, and DRAM and staging are both simply addresses to it. That design
is `docs/arch/sysnode/control-processor.md`.

**What this generalises is the attach, not the core.** The same tag array with a
different fill backend serves a unit at a mesh port, a foreign core with an AXI
master, or several units sharing one L2. The mechanism is proven one tier down;
what is missing is lifting it out and making the backend a parameter.

**And it is what makes CU control uniform with memory.** If `CU_INST` and
`CU_SIGNAL` become communication ranges (§5) rather than dedicated ports, then a
unit dispatches by storing and collects completions by loading, and there is no
part of the machine a client reaches by a side channel.

---

## 13. Open

- **Fill target** — DRAM, or the explicit staging that already exists. Staging is
  the more interesting answer and needs the aperture decode.
- **Arbitration** across slave ports, once there is more than one.
- **Ordering between the two classes.** A communication store that must not
  overtake the memory writes it announces is the doorbell problem again — and the
  interlink already solves its version, by holding an inbound doorbell until
  every write ahead of it has its response.
- **Whether the native port and the AXI port are one block with two faces**, or a
  native block with an AXI shim in front. The shim is cleaner; the cost is a
  handshake hop.
- **Cost**, which needs one out-of-context run before any number is quoted.
