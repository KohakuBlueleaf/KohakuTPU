---
title: The opcode map
summary: Which PE class owns each of RISC-V's four custom opcode majors, why, and where the encoding room actually is.
tags:
  - architecture
  - cpu
  - isa
---

# The opcode map

RISC-V reserves **four** custom opcode majors and no more. A *major* is the
7-bit `opcode` field's value; everything an implementation adds outside the
standard instruction set has to live inside one of the four the standard sets
aside for it.

Three PE classes in this machine add instructions: the
[RV32 PE](README.md) described in this directory, and the SIMD and SIMT classes
[KohakuMPE](../../../projects/kohakumpe/README.md) builds on it. They all draw
from that one pool **for the instructions RISC-V has not already
standardised**, so the allocation is recorded here rather than inside any
single ISA module — a table that lives in one class's source is not an
authority another class can check itself against.

Extensions that *are* standard cost nothing from this pool, which is not a
technicality: [`RV32M`](#rv32m-cost-none-of-them) is the one that has since
been built, on two classes.

| major | opcode | owner | carries |
|---|---|---|---|
| custom-0 | `0x0B` | **SIMD PE** | the packed-integer tier: memory, packed ALU, bitwise, shifts, dot and accumulators, moves, permute |
| custom-1 | `0x2B` | **SIMD PE** | the float tier. A build without the float format leaves the major unmapped, so a float instruction faults rather than landing in a decode case |
| custom-2 | `0x5B` | **SIMT PE** | the R-type groups: scalar ALU, scalar/vector moves, divergence, subgroup, the uniform-base and lane-linear memory forms |
| custom-3 | `0x7B` | **SIMT PE** | the I-type groups: scalar immediate ALU, scalar shifts, the two uniform branches |

All four are now spoken for.

## `RV32M` cost none of them

The multiply half of `RV32M` — `mul`, `mulh`, `mulhsu`, `mulhu` — is **not** in
a custom major on any class. It went where RISC-V already put it: the standard
`OP` major, `opcode = 0110011`, `funct7 = 0000001`, alongside the base
register-register group.

| | |
|---|---|
| major | `OP`, `0110011` — **standard, not custom** |
| `funct7` | `0000001` |
| `funct3` `000`–`011` | `mul`, `mulh`, `mulhsu`, `mulhu` — **built** |
| `funct3` `100`–`111` | `div`, `divu`, `rem`, `remu` — **decoded and refused**, an illegal-instruction fault |

**Two classes decode it, and the split is the same on both.** Verified against
the RTL rather than the assembler:

- the **RV32 PE** — `src/kohakuaccel/pe/rv32/core/rv_id.v` accepts `funct7` of
  `0000000` and `0100000` on the register-register group; a separate case after
  that one accepts `0000001` and sets the multiply control when `funct3[2]` is
  0, and raises `illegal` when it is 1. The SIMD PE's scalar half is this same
  decoder, so it behaves identically;
- the **SIMT PE** — `src/kohakumpe/simt/kht_predec.v` computes a multiply term
  and a divide term from the same `funct7`, split on `funct3[2]`, and the
  divide term is an input to `illegal`.

So the division half of `RV32M` is refused *by name* on both, rather than
falling through a default — the encoding is recognised and rejected, which is
what makes the fault a fault and not an aliased multiply.

The generated custom-major headers carry no `RV32M` constant, and that is
correct rather than an omission: they hold the custom-major tables, and `RV32M`
is not in either. The single place the encoding is decided is the two decode
files named above, which is also where every other standard-major opcode each
class accepts is decided.

Two consequences worth stating, because both are the reason this is recorded on
a shared page rather than in a per-class file:

- **The four custom majors are untouched by it.** Anyone counting remaining
  encoding room should not charge `RV32M` against the map above.
- **A compiler emits it unmodified** — `-march=rv32im` and nothing else. That
  is the property no custom-major extension in this machine has, and it is why
  standard encoding space is worth more than its size suggests
  ([microarchitecture](microarchitecture.md#why-minimal-scalar-float-is-the-wrong-purchase)).

On the SIMT PE, `RV32M` ships with the float tier rather than separately from
it — the two share a per-wave pending flag and a retire slot — so a build
without that tier faults the encoding rather than returning something quietly.

## Why the SIMT PE takes two majors

An I-type layout has no `funct7`, so an I-type **group holds exactly one
instruction**. The SIMD tier hit this and spent a whole `funct3` each on its
vector load and vector store. The SIMT PE needs eight I-type instructions —
immediate ALU, three shifts, two branches — and packing them into one major
alongside the R-type groups would have left three or four groups for everything
else.

The alternative was stealing three immediate bits as a sub-opcode, which buys
back a major at the price of 9-bit immediates and a permanent extra instruction
on every 32-bit constant a shader builds. That is a worse trade than spending a
major nobody else has a claim on.

## Why the SIMD majors were not reused

A SIMT build carries no SIMD tier, so `custom-0` and `custom-1` would be free
in it. They are left alone anyway, and the reason is forward-looking rather
than present: the SIMT PE is expected to selectively expose the SIMD tier's
packed-integer operations later, and if it does, it should reuse **those
encodings unchanged** — so the assembler, the golden model and the disassembler
share code instead of forking a second packed-integer table. A PE that ever
carried both tiers then never renumbers either set.

## This is not the constraint it looks like

Spending the last two majors is not spending the last room, and the distinction
matters because it is the one that decides whether a future extension is
blocked.

**The scarcity is in format-distinct instructions, not in instructions.** An
R-type group has a 7-bit `funct7`, so it holds **128** operations. The
extensions deliberately deferred today — atomics, texture, ray tracing — are
all R-type shaped: a destination, one or two sources, an operation selector.
Each of them fits in `funct7` room inside an existing group, or in one of the
unused `funct3` groups, without touching the major allocation at all.

`custom-3`'s `funct3` space is deliberately **not** filled. Leave it that way:
it is the only I-type room the machine has left, and an I-type instruction is
the one shape that cannot be squeezed in anywhere else.

## Where each table lives

| | |
|---|---|
| SIMD, custom-0 and custom-1 | `tests/pe/tools/rv_simd_isa.py`, with the float half in `rv_simd_isa_f.py` |
| SIMT, custom-2 and custom-3 | `tests/pe/tools/rv_simt_isa.py` |
| SIMT, the **standard** majors it accepts — including `RV32M` | `src/kohakumpe/simt/kht_predec.v`, which is decode rather than a table |
| RV32 PE and the SIMD scalar half, the standard majors — including `RV32M` | `src/kohakuaccel/pe/rv32/core/rv_id.v` |

Each custom table is the single source for its majors and generates its own RTL
decode header; a test regenerates and compares, so a hand edit fails rather
than quietly disagreeing. Neither table may allocate outside the majors this
page gives it.

The two standard-major rows are not tables and are not generated — they decode
an encoding RISC-V already fixed, so there is nothing to allocate and nothing
that could drift from a specification this project owns.
