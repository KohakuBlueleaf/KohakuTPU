---
title: The opcode map
summary: Which PE class owns each of RISC-V's four custom opcode majors, why, and where the room actually is.
tags:
  - architecture
  - pe
  - isa
---

# The opcode map

RISC-V reserves **four** custom opcode majors and no more. Every PE class in
this machine draws from that one pool **for the instructions RISC-V has not
already standardised**, so the allocation is recorded here rather than inside
any single ISA module — a table that lives in one tier's source is not an
authority the other tier can check itself against. Extensions that *are*
standard cost nothing from this pool, which is not a technicality:
[`RV32M`](#rv32m-cost-none-of-them) is the one that has since been built.

| major | opcode | owner | carries |
|---|---|---|---|
| custom-0 | `0x0B` | **SIMD PE** | the packed-integer tier: memory, packed ALU, bitwise, shifts, dot and accumulators, moves, permute |
| custom-1 | `0x2B` | **SIMD PE** | the float tier. A build without E8M15 leaves the major unmapped, so a float instruction faults rather than landing in a decode case |
| custom-2 | `0x5B` | **SIMT PE** | the R-type groups: scalar ALU, scalar/vector moves, divergence, subgroup, the uniform-base and lane-linear memory forms |
| custom-3 | `0x7B` | **SIMT PE** | the I-type groups: scalar immediate ALU, scalar shifts, the two uniform branches |

All four are now spoken for.

## `RV32M` cost none of them

The SIMT PE's per-thread multiply — `mul`, `mulh`, `mulhsu`, `mulhu` — is **not**
in a custom major. It went where RISC-V already put it: the standard `OP` major,
`opcode = 0110011`, `funct7 = 0000001`, alongside the base register-register
group the per-thread datapath already decodes.

| | |
|---|---|
| major | `OP`, `0110011` — **standard, not custom** |
| `funct7` | `0000001` |
| `funct3` | `000`–`011` = `mul`, `mulh`, `mulhsu`, `mulhu` |
| `funct3` `100`–`111` | `div`, `divu`, `rem`, `remu` — **decoded and refused**, an illegal-instruction fault |

Verified against the RTL rather than the assembler: `kht_predec.v` computes
`is_imul = is_op && (f7 == 7'b0000001) && (f3[2] == 1'b0)` and
`is_mdiv = is_op && (f7 == 7'b0000001) && (f3[2] == 1'b1)`, and `is_mdiv` is a
term of `illegal`. So the division half of `RV32M` is refused *by name* rather
than falling through a default — the encoding is recognised and rejected, which
is what makes the fault a fault and not an aliased multiply.

`kht_isa.vh` carries no `RV32M` constant, and that is correct rather than an
omission: the generated header holds the **custom-2 and custom-3** tables, and
`RV32M` is not in either. The single place the encoding is decided is
`kht_predec.v`, which is also where every other standard-major opcode the GPU
accepts is decided.

Two consequences worth stating, because both are the reason this is recorded on
this page rather than in a GPU file:

- **The four custom majors are untouched by it.** Anyone counting remaining
  encoding room should not charge `RV32M` against the map above.
- **A compiler emits it unmodified** — `-march=rv32im` and nothing else. That is
  the property no custom-major extension in this machine has, and it is why
  standard encoding space is worth more than its size suggests
  ([microarchitecture](microarchitecture.md#why-minimal-scalar-float-is-the-wrong-purchase)).

`RV32M` ships with the SIMT PE's float tier rather than separately from it — the
two share a per-wave pending flag and a retire slot — so a build without that
tier faults the encoding rather than returning something quietly. The controller
PE and the SIMD PE's scalar half decode neither half of `RV32M`: `rv_id.v`
accepts `funct7` of `0000000` and `0100000` on that group and nothing else.

## Why the GPU takes two

An I-type layout has no `funct7`, so an I-type **group holds exactly one
instruction**. The SIMD tier hit this and spent a whole `funct3` each on `vld`
and `vst`. The GPU needs eight I-type instructions — immediate ALU, three
shifts, two branches — and packing them into one major alongside the R-type
groups would have left three or four groups for everything else.

The alternative was stealing `imm[11:9]` as a sub-opcode, which buys back a
major at the price of 9-bit immediates and a permanent extra instruction on
every 32-bit constant a shader builds. That is a worse trade than spending a
major nobody else has a claim on.

## Why the DSP majors were not reused

A GPU build carries no SIMD tier, so `custom-0` and `custom-1` would be free in
it. They are left alone anyway, and the reason is forward-looking rather than
present: the GPU is expected to selectively expose the DSP's packed
integer operations later, and if it does, it should reuse **the DSP's own
encodings unchanged** — so the assembler, the golden model and the
disassembler share code instead of forking a second packed-integer table. A PE
that ever carried both tiers then never renumbers either set.

## This is not the constraint it looks like

Spending the last two majors is not spending the last room, and the distinction
matters because it is the one that decides whether a future extension is
blocked.

**The scarcity is in format-distinct instructions, not in instructions.** An
R-type group has a 7-bit `funct7`, so it holds **128** operations. The
extensions deliberately deferred today — atomics, texture, ray tracing — are all
R-type shaped: a destination, one or two sources, an operation selector. Each of
them fits in `funct7` room inside an existing group, or in one of the unused
`funct3` groups, without touching the major allocation at all.

`custom-3`'s `funct3` space is deliberately **not** filled. Leave it that way:
it is the only I-type room the machine has left, and an I-type instruction is
the one shape that cannot be squeezed in anywhere else.

## Where each table lives

| | |
|---|---|
| DSP, custom-0 and custom-1 | `tests/pe/tools/rv_simd_isa.py`, with the float half in `rv_simd_isa_f.py` |
| GPU, custom-2 and custom-3 | `tests/pe/tools/rv_simt_isa.py` |
| GPU, the **standard** majors it accepts — including `RV32M` | `src/kohakumpe/simt/kht_predec.v`, which is decode rather than a table |
| controller and DSP scalar half, the standard majors | `src/kohakuaccel/pe/rv32/core/rv_id.v` |

Each custom table is the single source for its majors and generates its own RTL
decode header; a test regenerates and compares, so a hand edit fails rather than
quietly disagreeing. Neither table may allocate outside the majors this page
gives it. The two standard-major rows are not tables and are not generated —
they decode an encoding RISC-V already fixed, so there is nothing to allocate
and nothing that could drift from a spec this project owns.
