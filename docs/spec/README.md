---
title: Specification index
summary: The normative contracts of KohakuAccel — the compute-unit port, the flit format, the instruction payload split, the memory protocol, the control registers, and every module parameter.
tags:
  - spec
  - normative
  - index
---

# Specification

These documents are **normative**. A compute unit that satisfies everything
stated here works on the framework. One that does not, does not — the failure
mode is not a rejected build, it is a mesh that hangs or a tile that is quietly
wrong.

Everything here describes the framework. Where a choice belongs to
[KohakuTPU](../projects/kohakutpu/) — the reference accelerator built on it —
it is marked as **one example of spending a free field**, never as the contract.
A second accelerator may spend those fields differently and is still conformant.

For why the framework is shaped this way, read [arch/](../arch/). For how to
plug into it, read [integrate/](../integrate/). This tree tells you what the
bits are.

## Three kinds of statement, and you must know which you are reading

A reader of a specification has to know what they are **allowed** to violate.
Every statement in this tree is one of three kinds, and each is marked where it
appears.

**These are the tree's four kinds minus one.** [The constitution](../README.md)
says everything in KohakuAccel is *fixed protocol*, *customizable addon*,
*convention* or *yours*; this tree carries the first three and never the fourth,
because **a specification does not describe something that is yours.** The moment
a thing is yours there is nothing left to specify — which is why the compute
unit's datapath, its memories and its instruction semantics appear here only as
the boundary they present, never as a requirement.

| Kind | What it is | Can you change it? |
|---|---|---|
| **Fixed** | The protocol. Flit format, port handshake, memory request and response encoding, retry and credit, cross-mesh encapsulation. Anything stating a signal, a bit position or a handshake rule. | **No.** Change it and you are not on the framework any more. |
| **Addon** | A slot that ships working and exists to be swapped. The memory agent's transform stage, memory-agent-side staging, the endpoint's staging adapter. | **Yes** — that is what the slot is for. The interface around it is Fixed; what you put in it is not. |
| **Convention** | An idiom: a recommended layout, a tagging discipline, a way we did it. Instruction-bit budgets, unit-to-unit payload shapes, the local-buffer fill idiom. | **Yes.** Nothing checks it. Some are nonetheless *effectively binding* because the memory agent will hand you data in a particular shape whether or not you asked; each is labelled with which. |

Two rules follow, and they are on the authors of this tree rather than on you:

- A table **MUST NOT** mix kinds without a column that distinguishes them.
- A Convention **MUST** say in one line whether the memory agent forces it.

A reader who cannot tell the difference either violates a real contract or wastes
weeks obeying advice.

### What the framework does not specify at all

**Your compute unit is yours, its memory system included.** Nothing in this tree
fixes the width, count, primitive or read latency of a unit's local storage, and
nothing ever will. The two units in the reference project share none of it — one
holds operands in two 928-bit RAMs beside an accumulator tile at a different
read latency, the other in a 256-bit RAM plus a block-RAM instruction memory and
three mirrored register-file RAMs.

What is fully defined is **how you receive and how you send**: the port, the flit,
and the request/response encoding. Everything on your side of that boundary is a
design decision the framework declines to make.

The best worked example of the Fixed/Convention line inside one topic is
unit-to-unit transfer: the envelope is Fixed, the payload inside one mesh is
Convention, and the moment the transfer crosses a mesh boundary the whole thing
becomes Fixed — because the interlink has to encapsulate and route it, and it
cannot forward a shape it does not know. See
[memory-protocol.md](memory-protocol.md) §6.0 and §9.3.

## The documents

| Document | What it covers | Kinds inside |
|---|---|---|
| [compute-unit-port.md](compute-unit-port.md) | Every signal a compute unit presents, every obligation it meets, and everything it may never do. | Fixed throughout. §10 is illustration. |
| [flit-format.md](flit-format.md) | The flit: header fields, bit positions, message types, per-type payload layouts, and who owns each field. §1 separates what is protocol from what follows `FLIT_WIDTH`. | Fixed. One Convention pocket in §4.7.1. |
| [instruction-encoding.md](instruction-encoding.md) | The three owners of instruction bits, and the split inside `CU_INST`. | Fixed for the header and the memory descriptors; Convention for how a unit spends its own payload. |
| [memory-protocol.md](memory-protocol.md) | Request, response, descriptor and streaming traffic against the memory agent, plus the ordering guarantees and their absences. | Fixed for the encoding and ordering; one Addon (§10); several Conventions the agent forces (§3.2.3). |
| [control-registers.md](control-registers.md) | Four register surfaces: the `CU_CTRL` block every unit answers, the orchestrator's AXI map, the mover's and the interlink's windows, and the RV64 control complex's host window and control region. | Fixed. One Convention (the dispatch order, §2.3). |
| [transform-slot.md](transform-slot.md) | The shared transform bank on the mover's read-return path: where it sits, how an occupant is selected, and what the port and geometry contracts are. | Fixed interface, Addon occupant. |
| [parameters.md](parameters.md) | Every parameter of every framework module: type, default, effect, legal range. | **Fixed** about ranges and meanings; the values are **Yours**. |

**Two processors, one node.** The system node ships a control processor, and
which one is a build-time choice: `CPU_RV64 = 0` selects the RV32 complex, which
sits on the mesh as a compute unit; non-zero selects the RV64 complex, which does
not, and which the host reaches through a dedicated window instead. Where that
choice changes a contract, the page says so — [parameters.md](parameters.md)
§5.1, [control-registers.md](control-registers.md) §6–§7, and
[transform-slot.md](transform-slot.md). Everywhere else it changes nothing: the
flit format, the port, the memory protocol and the instruction encoding are
identical either way. The architecture is
[arch/cpu/](../arch/cpu/README.md).

## What to read, in what order

**Writing a compute unit — you MUST read:**

1. [compute-unit-port.md](compute-unit-port.md) in full. It is the contract.
2. [flit-format.md](flit-format.md) §1–§4 — the header and the type codes. Skip
   the per-type payload tables for message classes you do not send.
3. [instruction-encoding.md](instruction-encoding.md) — before you allocate a
   single instruction bit.
4. [memory-protocol.md](memory-protocol.md) §3–§5 and §7, if your unit reads or
   writes memory itself, and §3.2.3 in particular — it is the one place the
   agent's shape reaches into your design. §6 if your unit sends or receives
   `CU_DATA`, which includes every unit that is fed by another unit rather than
   by memory. §7 regardless: the ordering non-guarantees apply to everything.

**Lookup only, read when you need the number:**

- [control-registers.md](control-registers.md) — needed when you write the
  driver or the bring-up script, not when you write the datapath. The framework
  answers the `CU_CTRL` block on your behalf; §1.4 is the one part that reaches
  your logic. §6 and §7 are needed only if your node carries the RV64 control
  complex and you are loading or driving software on it.
- [transform-slot.md](transform-slot.md) — needed when memory holds your
  operands in a format your datapath does not want, and only then. A unit that
  reads what it was given never meets the slot.
- [parameters.md](parameters.md) — needed when you instantiate or floorplan.

## Conventions

**Normative language.** MUST and MUST NOT are absolute: violating one is a
protocol error, and the framework's behaviour afterwards is undefined. SHOULD
marks a requirement with known, stated exceptions. MAY marks a genuine choice
that the framework will not take away later.

**Bit numbering.** Verilog convention throughout: `[hi:lo]`, MSB first, bit 0 the
least significant. `f[255 -: 40]` is the 40 bits from 255 down to 216, which is
how the RTL writes it. Flit fields are given twice where they differ: once as an
expression in `FLIT_WIDTH` and `POS_WIDTH`, and once as the concrete positions at
the reference build's `FLIT_WIDTH = 288`, `POS_WIDTH = 4`.

**A width is not a contract unless this tree says so.** `FLIT_WIDTH`,
`POS_WIDTH`, `DATA_W`, `ADDR_W` and the queue depths are build-time parameters,
and the numbers this tree prints beside them are what the reference build sets.
Three claims are distinct and each page marks which it is making:

- what is **true at every legal value** — protocol, and the only kind you may
  build a contract on;
- what **follows the parameter** because the RTL computes it;
- what is a **literal in the RTL** that does not track the parameter, and will
  therefore silently mean something else if you change it.

The third is the dangerous one and it is not rare — every flit payload field
position is in it. [flit-format.md](flit-format.md) §1 works the three through
for the flit; [parameters.md](parameters.md) gives the legal range for each
parameter and says where a value is genuinely constrained rather than merely
untried.

**Kind.** Sections and rows carry one of **Fixed**, **Addon** or **Convention**,
as defined above. An unmarked normative statement is Fixed.

**Ownership.** Every field is one of:

| Marking | Meaning |
|---|---|
| **framework** | The framework reads or writes it. A unit that repurposes it breaks routing, reassembly or dispatch. |
| **reserved** | Nothing reads it today. It MUST be transmitted as zero, so a later reader can be added without auditing every sender. |
| **unit** | Yours. The framework never inspects it, and never will without a revision of this spec. |

**No measurements.** No Fmax, LUT count or utilisation figure appears in this
tree. Those describe one accelerator on one part and live with the project that
produced them, in [projects/](../projects/).

## Where the source of truth is

The RTL is the source of truth and this tree is checked against it, not derived
from an earlier document. Where a header comment, a `noc_pkt.vh` macro or an
earlier pre-reframing snapshot disagrees with the silicon, the
silicon wins and the disagreement is recorded in the relevant document under
"Known divergences". Do not resolve one of those by reading the other file.

**A contract names a module and a signal. A line number is a hint, and only a
divergence carries one.** The distinction is deliberate and it is about what
rots:

- A **requirement** — what a signal must do, what a field means, what a
  handshake obliges — is stated as module and signal, never as a line. A
  refactor moves lines; it does not move the contract, and a contract whose
  evidence has to be re-verified after every edit is a worse contract.
- A **divergence** is a claim that one specific expression in one specific file
  disagrees with this tree. There the exact spot *is* the evidence, and naming
  only the module would make the claim unfalsifiable. Those carry a line
  number — and each also quotes the expression, so a reader who finds the line
  has moved can still find the code by searching for it.

Treat the file as authoritative and the line number as a pointer that was true
when it was written.
