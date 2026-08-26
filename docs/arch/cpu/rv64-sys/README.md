---
title: RV64 system core
summary: A 64-bit RISC-V core built to host a runtime rather than a kernel loop — what it is, why the machine carries it as well as the RV32 controller, the privilege and paging it now implements, and the two configurations it ships in.
tags:
  - architecture
  - cpu
  - rv64
---

# RV64 system core

`src/kohakuaccel/pe/rv64-sys/` — an in-order, single-issue **RV64IMA + Zicsr**
core with a branch predictor, machine, supervisor and user modes, Sv39 paging
for both fetch and data, a write-back L1, and a trap and interrupt model with
delegation.

Its job is to be the thing on the card that **runs continuously and decides what
happens next**: walking a dependency graph, replaying a recorded command
program, servicing an interrupt, keeping time. That is a different job from the
[RV32 controller PE](../rv32-pe/README.md), which runs one kernel at a time
inside a compute unit and reports when it is done.

## Where it sits

Two places, and they are different products rather than two settings of one:

```
   ─── as a mesh compute unit ──────────────────────────────────────────

     fabric ──▶ compute-unit port ──▶ rv64_sys_pe
                                        core + imem + scratchpad
                                      kicked, runs, reports a word

   ─── as the system node's processor ──────────────────────────────────

     host AXI ──▶ rv64_syscore ──▶ node port ──▶ MAG ──▶ DRAM, staging,
                    core + MMU + L1                       other meshes
                  boots once, runs forever
                         │
                         └── mailbox ──▶ the node's hub ──▶ compute units
                             dispatch out, completions back as interrupts
```

**The second of those is complete at the port level and is still a parameter,
not the shipped default.** The processor boots, runs, reaches memory through
MAG, commands the node's mover, dispatches to compute units through a mailbox
and takes their completions as interrupts, and rings the interlink's doorbells.
Every port of the complex is connected inside the node — [what is wired at the
node](integration.md#what-is-wired-at-the-node) is the list. But selecting it is
a parameter (`CPU_RV64`) that **defaults to off**, so a build that does not ask
for it ships the RV32 control complex, and no routed result exists for either.
[arch/sysnode](../../sysnode/README.md) owns the node-level picture.

A **compute unit** is anything that attaches to one fabric port, accepts
instructions one at a time and signals retirement
([arch/README](../../README.md#what-a-compute-unit-is)). A **flit** is the
288-bit unit the fabric carries. A **kick** is the instruction that starts a
unit running; a **completion** is the flit it sends back when it stops.
**MAG** is the system node's memory access half — the block that turns
descriptors into DRAM traffic and carries cross-mesh writes
([arch/sysnode](../../sysnode/README.md)).

## Why the machine has two processors

They are not a big one and a small one. They answer to different lifecycles,
and the lifecycle is what forces almost every other difference.

| | [RV32 controller PE](../rv32-pe/README.md) | RV64 system core |
|---|---|---|
| lifecycle | kicked, runs to completion, reports a word | boots once, runs until stopped |
| a fault is | the completion — the unit halts and says why | a trap into a handler, at whichever level it is delegated to |
| interrupts | none; the unit is never interrupted | timer, software and external lines, at machine or supervisor level |
| CSRs | none exist | the machine and supervisor sets, plus a free-running `mtime` |
| privilege | none | **machine, supervisor and user**, with `medeleg`/`mideleg` delegation |
| translation | none | **Sv39**, on both fetch and data, when `satp` says so and the level is not machine |
| arithmetic | RV32I: no multiply, no divide, no float | RV64**IM**: multiply and divide, still no float |
| atomics | none — exclusive access is ownership and push | the **A** group, and it is what makes a shared counter expressible |
| address space | its own windows | the whole 40-bit card address space, through a node port |
| predictor | a 32-entry BTB, right for one hot loop | BTB + gshare + a return-address stack, for call-dense code |
| what it counts as | a compute unit | the node's processor, or a compute unit |

Two of those rows carry most of the weight:

**An operating system never completes.** The framework's compute-unit shell
implements *someone kicks me, I run, I report a 32-bit word*. There is no word
for a program that is meant never to end, and no moment at which to send it.

**Atomics are not optional for a scheduler.** The node's staging store —
on-chip memory the mover and the interlink share — is multi-writer but
**single-reader**, which makes it a mailbox rather than shared memory. It can
express join and release; it cannot express mutual exclusion or a shared
counter. Without the A group the machine cannot construct a multi-writer
location outside DRAM at all.

## The two configurations

One core, two wrappers, and they present different contracts:

| | `rv64_sys_pe` — mesh compute unit | `rv64_syscore` — the node's processor |
|---|---|---|
| fabric shell | `noc_cu_base`, plus a loader and a kick FSM | **none** — fused to MAG |
| how it is loaded | `CU_DATA` flits, then a `CU_INST` kick | host AXI writes, then a boot register |
| how it stops | a control-region store; the shell sends a `CU_SIGNAL` | a control-region store; the host reads a status register |
| memory reach | its own instruction window and scratchpad, nothing else | scratchpad, control region, and everything out the node port |
| MMU | none instantiated — pays nothing for one | `rv64_mmu`, serving **both fetch and data** ([memory-system](memory-system.md#sv39)) |
| L1 | none | `rv64_l1`, 64 lines of 32 bytes, write-back |
| atomics | `HAS_ATOMIC`, and the instantiation leaves it on | required, and on |
| the mover | not attached | attached — `mv.go` is a store into the control region |
| the fabric | `noc_cu_base` receives its image and its kick; the send path is tied off | a **dispatch mailbox** in the control region, on the node's hub ([integration](integration.md#the-dispatch-mailbox)) |

Both instantiate the same `rv64_core`, **including its privilege register and
its `satp`** — those are architectural state and live in the core's CSR file.
What the core does not have is a **translator**: its datapath is a
physical-address machine, and the TLB and page-table walker live in the wrapper.
That is why the mesh configuration carries none of that machinery and none of
its area, and why the privilege levels it does carry buy it no isolation — with
no MMU there is nothing for a `U` page to mean.

`rv64_mag_pe` is the third top in the tree and is not a third configuration: it
is `rv64_syscore` plus the node's memory **mover** and its transform slot, both
of which belong to the node whatever processor sits in it —
[integration](integration.md#the-node-complex--rv64_mag_pe).

## The pages

| Page | What is in it |
|---|---|
| [architecture](architecture.md) | the contract: the ISA as implemented, the M/S/U privilege model and delegation, the CSR set with its WARL masks, the cause table, when a trap's effects land, the two address spaces, and what it deliberately lacks |
| [microarchitecture](microarchitecture.md) | every stage opened up, the sub-pipeline inside each, the forwarding network and why a read-first array needs three sources, every stall and every flush, the trap's split cycle, the predictor, and the multi-cycle units |
| [memory-system](memory-system.md) | Sv39 and the hardware walk, one MMU serving fetch and data and the rules that keep them apart, the fetch page register, the TLB entry and why it is 57 bits, the L1, the node-port arbiter, and the ordering the core publishes |
| [integration](integration.md) | both wrappers: what replaces the compute-unit shell and why, the dispatch mailbox, the loader and the kick, the host window, the node port, and the node complex |
| [performance](performance.md) | Dhrystone, IPC, the fabric-latency sweep, and LUT/Fmax — each with the part, the tool, the mode and the script that produced it |
| [programming](programming.md) | link maps and the build lines a node program needs, the control region as code, the dispatch mailbox and the interlink doorbell, writing a trap handler across three privilege levels, and how to start a program |

If the question is **"what can this thing actually do?"** rather than how any of
it is built, start at
[arch/sysnode/abilities](../../sysnode/abilities.md) — the node's ability
reference, written against the RTL, with the register maps a program needs and
the test behind each claim. It covers this processor as part of a whole node;
the pages above are the processor on its own terms.

If you are writing software for it, start at [programming](programming.md) and
stand it on [architecture](architecture.md). If you are deciding whether to put
one in a design, [performance](performance.md) then
[integration](integration.md).

## Fixed protocol, parameter, or yours

| Thing | Category |
|---|---|
| the compute-unit port, the flit, the `CU_CTRL` registers | **fixed protocol** — the fabric's, not this core's: [spec](../../../spec/) |
| the control region layout and the exit-by-store protocol | **fixed protocol** of this core — [architecture](architecture.md#program-exit-is-a-store) |
| the dispatch mailbox's register map and completion layout | **fixed protocol** of `rv64_syscore` — [integration](integration.md#the-dispatch-mailbox) |
| the host window register map | **fixed protocol** of `rv64_syscore` |
| the Sv39 page-table format, `satp`, the CSR addresses and the cause codes | **fixed protocol** — RISC-V's privileged specification, narrowed where WARL permits |
| the RV64IMA + Zicsr encoding | **fixed protocol** — RISC-V's, and the point of using it |
| `HAS_ATOMIC`, `MEM_PRIM`, `L1_LINES`, `TLB_ENTRIES`, `IMEM_WORDS`, `SPAD_WORDS`, `SPAD_STYLE`, the predictor's four sizes | **parameters** — the defaults are what is measured: [integration](integration.md#parameters) |
| the cached/uncached split of the node address space | **convention** — a bit test in the wrapper, and it is the wrapper's to change |
| what the program does | **yours** |

## What this core does not own

| Concern | Whose |
|---|---|
| the flit, the link, the router, the port handshake | [noc](../../noc/) |
| descriptor encoding, write slots, response tagging, the mover's command set | [sysnode](../../sysnode/) |
| the 40-bit card address map, and what the aperture bit selects | [address-map](../../../address-map.md) |
| where the core lands on the die and at what clock | [physical](../../physical/) |
| what a compute unit computes when this core dispatches to it | the unit's author |

## What it deliberately does not do

- **No floating point.** No `f0..f31`, no `fcsr`, no rounding mode. Not "not
  yet": there is no float register file to name. Arithmetic in this machine
  lives in the wide datapaths.
- **No compressed instructions.** `C` is absent; every instruction is 4 bytes
  and the fetch path assumes it.
- **No multiple harts.** One core per wrapper. `LR`/`SC` carry a single
  reservation because there is no second hart to lose it to.
- **No PMP, no ASID, no `stimecmp`, no vectored trap entry.** Isolation is Sv39
  and nothing else; an address-space switch sweeps the whole TLB; and the timer
  cannot usefully be delegated to a supervisor that has no compare register of
  its own ([architecture](architecture.md#delegation)).
- **No instruction TLB.** Fetch translates through a single registered page
  mapping, refilled on a page crossing, and the data port owns the MMU when both
  want it ([memory-system](memory-system.md#one-mmu-two-requesters)).
- **No outbound queue on the mailbox.** One dispatch flit at a time, and a
  16-deep completion queue whose overflow is reported rather than prevented
  ([integration](integration.md#the-dispatch-mailbox)).
- **No cache maintenance from software.** The L1's flush and invalidate inputs
  are tied off in the wrapper, so a program cannot force a dirty line out
  ([memory-system](memory-system.md#what-the-core-publishes-about-ordering)).
