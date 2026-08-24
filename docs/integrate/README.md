---
title: Integrating with KohakuAccel
summary: The framework removes the connection problem, not the design work — what that means concretely, which files are yours, and whether your workload belongs here at all.
tags:
  - integrate
  - overview
---

# Integrating with KohakuAccel

This section is the surface you build against. Everything else in `docs/` either
describes machinery you are given ([arch/](../arch/README.md)), states a contract
you must satisfy ([spec/](../spec/README.md)), or teaches a practice you will
need ([workflow/](../workflow/README.md)). This is the part that says what to
type.

Read this page first. It answers three questions, and the last one is the most
important: *what does the framework actually do for me*, *which files are mine*,
and *should I be here at all*.

---

## 1. What the framework actually removes

Not the design work. **The connection problem.**

You design a whole compute unit: the datapath, its memories, its pipeline, what
its instructions mean. That is the interesting part and the framework has no
opinion about it — the two units in the reference project agree on almost none of
it, and both are first-class citizens of the same mesh
([what-you-own.md](what-you-own.md) §3).

What is fully defined is **how you receive and how you send**. The port is given.
The protocol across it is given. Dispatch, credits, completion, faults,
discovery, memory requests, unit-to-unit transfer, cross-mesh addressing — all
given, all identical for every accelerator anyone would build here. That work is
unglamorous, it is where the silent failures live, and you do not have to work it
out.

Alongside the protocol come **worked examples and conventions**: two production
units of deliberately different shape, two minimal reference units, and a written
account of which design idioms are forced by the memory agent and which are
genuinely free. A protocol tells you what is legal. The conventions tell you what
is wise, which is the part that usually has to be learned by getting it wrong.

---

## 2. Four categories, not two

"Yours versus ours" is two categories where there are four, and the two in the
middle are where the useful decisions live.

| | what it is | may you change it |
|---|---|---|
| **Fixed protocol** | flit format, port handshake, memory encoding, credit and retry, cross-mesh encapsulation | **No.** Change it and you are off the framework |
| **Customisable addon** | ships working, designed to be swapped: the transform stage in the memory agent, staging inside it, the adapter in an endpoint's link | **Yes.** That is what the slot is for |
| **Convention** | how to design a well-behaved unit, with worked examples — some forced by the memory agent, some free | **Follow or don't**, but know which is which |
| **Yours** | datapath, memory structure, instruction semantics, pipeline depth | **Entirely** |

**[what-you-own.md](what-you-own.md) is that table in full**, with each row worked
out and each convention marked forced or free. It is the page to read before you
start designing, and the one to come back to when you are about to change
something the framework already had an answer for.

Concretely, for a project called `myaccel`, the files that are yours are:

```
    myaccel_cu.v          your compute unit: datapath, memories, sequencer,
                          wrapping noc_cu_base for the port
    <your datapath>.v     whatever it is built from -- one file or forty
    myaccel_xform.v       your read-path transform, if your operands are not
                          stored in the form your datapath wants
    myaccel.map           the mesh picture
    myaccel/isa.py        how a shape becomes instruction words
    myaccel/device.py     what a myaccel machine contains
    myaccel/kernels.py    scheduling policy: what to run where, in what order
```

Note what is *not* in that list on our side: the local memories. They are inside
your unit, they are your design, and the framework has no template for them —
see [what-you-own.md](what-you-own.md) §3.

The files that are **never** yours, in the sense that editing them is a signal
you have taken a wrong turn:

```
    src/kohakuaccel/
      noc/       noc_cu_base.v, noc_router.v, noc_inport.v, noc_outport.v,
                 noc_orchestrator.v, noc_pkt.vh
      sysnode/   mag.v, the memory mover, the control processor, the interlink
      axi/       the station bus, links, axi_n1.v and the AXI plumbing
      common/    sync_fifo.v, async_fifo.v, kohaku_sdpram.v, sb_skid.v
```

`common/` is the exception that proves the rule: you do not *edit* it, you
*instantiate* it. Naming a memory primitive through `kohaku_sdpram.v` rather than
inferring one from a `reg` array is a convention with real consequences — see
[what-you-own.md](what-you-own.md) §3.

If you are building a flit header by hand outside your own message types, or
adding a case to a router, or teaching the orchestrator about your opcodes, stop
— the framework already has a way to do that, described in one of the pages
below.

> **The quantiser has moved out.** It was `mx_quant.v` inside the framework's
> package, one project's number format wired into a framework slot. It is
> `src/kohakutpu/transform/mx_quant.v` now, reached through `xform_bank` — the
> one module name the framework fixes — and `src/templates/transform/` supplies
> an identity bank so a framework-only build elaborates with no project source.
>
> **One coupling is still on the wrong side**, and it runs the other way: the
> SIMD PE under `src/kohakuaccel/pe/rv32/simd/` instantiates arithmetic that
> exists only under `src/kohakutpu/`. See below.

### Where the boundaries fall

```
    host ── XDMA / JTAG-AXI ── AXI fabric ── orchestrator ── mesh
                                    │                          │
                                   DDR4 ──── MAG ──────────────┤
                                              │                │
                                    ┌─────────┴──────────┐     │
                                    │ transform slot     │     │
                                    │ ADDON: yours       │     │
                                    └────────────────────┘     │
                                                               │
                                            ┌──────────────────┴───────┐
                                            │  router local port       │
                                            ├──────────────────────────┤
                                            │  port + protocol   FIXED │
                                            ├──────────────────────────┤
                                            │  sequencer, memories,    │
                                            │  datapath, pipeline      │
                                            │                   YOURS  │
                                            └──────────────────────────┘
```

The port is a contract, not a container: `noc_cu_base` holds the mesh-facing side
so that a unit conforms by construction, and everything below it — including all
of the unit's storage — is designed by you.

---

## 3. What a project is

A project is a compute unit, a mesh map, a compiler back end, a device model and
— if its operands are not stored in the form the datapath wants — a read-path
transform. It is *not* a fork of the framework. Nothing in a project should have to be
merged back, and nothing in the framework should have to know the project's
name.

The directory shape that follows from that, as the tree has it today:

```
    src/kohakuaccel/     the RTL framework. Shared by every project.
      noc/               mesh, router, flit protocol, compute-unit port
      sysnode/           memory agent, mover, control processor, interlink
      axi/               station bus, links, AXI plumbing
      pe/rv32/           the CPU PE, and the SIMD PE behind SIMD_EN
      common/            FIFOs, named memory primitives
      verif/             bench-only models
    src/templates/       a conforming CU, a transform occupant, an endpoint
                         adapter — each with a bench
    src/examples/saxpy/  the example project's RTL half

    src/kohakutpu/       a project: matmul, vector, transform occupants, and
                         the tops generated for it under top/generated/
    src/kohakumpe/       a project: the SIMT PE

    compiler/kohakuaccel/  the compiler framework   compiler/kohakutpu/  project
    driver/kohakuaccel/    the driver framework     driver/kohakutpu/    project
```

The mesh generator is `scripts/py/gen_mesh.py` and a project keeps its ship
assemblies under its own tree. [software-stack.md](software-stack.md) §6 is
explicit about which couplings are cut and which are still open.

> **The separation is by directory, not by repository.** Framework and projects
> share one tree, one test suite and one build flow, so nothing enforces the
> split except the rule and the one check that measures it
> (`driver/tests/test_isolation.py`, for the software half). A second project
> would sit beside `kohakutpu/` rather than forking anything.
>
> **One coupling is not cut**, and it runs from the framework INTO a project:
> the SIMD PE under `src/kohakuaccel/pe/rv32/simd/` instantiates `vec_alu`,
> `vec_dsp`, `vec_delay`, the four `vec_cvt_*` converters and two helpers from
> `mx_fpacc.v` — all under `src/kohakutpu/`. Every build list that carries the
> SIMD PE carries the reference accelerator's arithmetic with it, so the
> framework does not build alone until that arithmetic moves down.

**KohakuTPU is one project built on this framework** — an MXFP7 tensor
accelerator. It appears throughout these pages as a worked example and is always
labelled as one. Nothing here requires it to exist.

---

## 4. Does your workload fit

Saying no here costs an afternoon. Finding out after floorplanning costs weeks.
The framework assumes a specific shape of computation, and the assumption is
load-bearing in the memory agent, the dispatch path and the mesh alike.

### It fits when

**Work decomposes into units that stream operands in, compute, and stream
results out.** A unit is handed a description of where its operands are, fetches
them, computes for a while, and writes results somewhere. This is the shape
`noc_cu_base` presents and the shape MAG serves.

**A unit's working set fits on-chip for the duration of one step.** There is no
cache between your unit and DRAM. What you fetch, you hold, in memory you
declared inside your unit. If a step needs more than that, the step is too big
and must be split by the compiler.

**Addresses are known ahead of time.** An operand fetch is a *descriptor*: base,
count, layout. MAG walks the address sequence itself. This is what makes one
flit produce a whole burst instead of one request per word — and it only works
if the sequence is known when the instruction is issued.

**Units are independent within a step.** They synchronise between steps, through
the host or through explicit unit-to-unit transfers, not inside one. The mesh
interleaves traffic from different senders at a shared port; there is no
ordering between two units except the one you construct.

### It does not fit when

**You need pointer chasing or data-dependent addressing.** A descriptor cannot
express "read this, then read where it points". You would issue one instruction
per dereference and the dispatch path would dominate. Graph traversal, sparse
gather with runtime indices, and anything hash-driven belong somewhere else.

**Two units need tight low-latency coupling.** If unit A must see unit B's
result within a handful of cycles, they are one unit. A mesh hop is a hop, and a
round trip through the network is not a pipeline stage. Write the bigger unit;
the framework does not charge you extra for it, and the cost of an endpoint is
mostly the router it needs, not the port itself
([mesh-topology.md](mesh-topology.md) §4).

**You want cache coherence between units.** There is none, at any level. Two
units that write the same DRAM line will produce whichever result the memory
agent happened to serve second. Ownership is a compiler-side property here, and
if your problem cannot be partitioned so that it is, this is the wrong machine.

**Your kernels are small enough that dispatch dominates.** Every instruction
costs a flit into the mesh and a completion flit back, and every batch costs a
host round trip. If the work between those is short, you are measuring the
control plane. Fuse until it is not, or accept that the accelerator is idle
most of the time.

### The honest test

Write down one step of your workload as: *these addresses in, this arithmetic,
these addresses out, no communication in the middle*. If you cannot, the answer
is no, and it is better to know now.

---

## 5. Where to go next

Seven pages, and they are roughly in the order you will need them.

| page | answers |
|---|---|
| [what-you-own.md](what-you-own.md) | What is fixed, what is a slot I may fill, what is convention, and what is mine? Which conventions are forced by the memory agent? |
| [compute-unit.md](compute-unit.md) | What port do I present, how do I fetch and return data across it, and what do the two real units do that made them easy? |
| [instruction-set.md](instruction-set.md) | Which bits do I own, which instruction sets already exist that I should be *using*, how do I report done and failed, and how do I keep the encoding sane for a compiler? |
| [addon-slots.md](addon-slots.md) | What is a slot actually made of, how do I fill one, and what does it take to turn a hardcoded part into one? Worked from real source. |
| [mesh-topology.md](mesh-topology.md) | How many units, on how many routers, and how does that interact with SLR boundaries and timing closure? |
| [multi-mesh.md](multi-mesh.md) | My machine has several meshes and they do not share memory — what changes in the IR, where is a split decided, and what does a collective cost? |
| [software-stack.md](software-stack.md) | What must the host side do for any project, what is project-specific, and what is not frameworkised yet? |
| [conformance.md](conformance.md) | How do I know I am right, at what level, in what order? |

A first unit is roughly: read [what-you-own.md](what-you-own.md), then
[compute-unit.md](compute-unit.md) with one of the two production units open
beside it. Get something small through the port-protocol bench before you make
the datapath interesting — the protocol is the part that fails silently, and
instruction encoding and topology are decisions you can revise afterwards.

The normative contract for the port lives in
[spec/compute-unit-port.md](../spec/compute-unit-port.md). These pages are the
guide; that one is the law. Where they disagree, the spec wins and this page is
a bug.

---

## 6. Open questions

- **There is no project template to copy.** A new project infers its shape — unit
  skeleton, map, device description, bench wiring — from KohakuTPU. A skeleton
  that builds and runs on day one would remove that inference. What exactly it
  contains is not settled.
- **`CU_TYPE` has no registry.** It is a 16-bit field published in `CU_CAPS` and
  every unit picks its own. Nothing detects a collision, and a collision would
  make two different units indistinguishable to enumeration. Whether the
  framework should own an allocation, or projects should namespace themselves,
  is undecided.
- **`CU_VERSION` is documented as a mesh-wide build number**, bumped in every
  endpoint whenever any ISA or datapath changes (`src/kohakutpu/vector/vec_cu.v`
  says so in a comment). That convention is a project's to keep, and nothing
  enforces it. Whether the framework should derive it instead is open.
