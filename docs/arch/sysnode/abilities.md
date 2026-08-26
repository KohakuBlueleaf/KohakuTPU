---
title: SysNode ability reference
summary: What a system node can do as a standalone system — the processor, its memory, the mover, the interlink and doorbells, the dispatch mailbox, the host window — stated as shipped, with the register maps a program needs and the tests that prove each ability.
tags:
  - architecture
  - sysnode
  - reference
---

# SysNode ability reference

This page answers one question: **if you take a system node as a system in its
own right, what can it do?** It is a reference, not a design description — the
mechanisms are in [control-processor](control-processor.md),
[memory-port](memory-port.md), [edge-and-control](edge-and-control.md) and
[arch/cpu/rv64-sys](../cpu/rv64-sys/README.md); the register-level contract is
[spec/control-registers](../../spec/control-registers.md). Every ability below
names the program or bench that exercises it, because an ability without a test
is a claim.

**Kind:** *fixed protocol* for the register maps and the address rules,
*convention* for the software sequences shown.

## 1. What a system node is

A **system node** (`sysnode`) is the control half of one mesh: a RISC-V
processor, the memory agent that owns the mesh's DRAM and its 2 MB on-chip
**staging** store, a descriptor-driven **mover** with a transform slot, and —
when the node is one of several — an **interlink** to the neighbouring meshes.
Compute units sit on the mesh's network and are commanded from here; this page
is about the node alone.

The configuration described is the one with the **RV64 control complex**
(`CPU_RV64 = 1`). It ships as:

- RTL under `src/kohakuaccel/sysnode/` and `src/kohakuaccel/pe/rv64-sys/`;
- a Verilator model of one node (`rv64_syscore` and `rv64_mag_pe` benches) and
  of **two nodes on one interlink** (`rv64_node_pair`), driven by C programs
  built with `riscv64-unknown-elf-gcc`;
- out-of-context synthesis on `xcvu13p-fhgb2104-2L-e` (§12).

**It does not yet ship inside a generated card top.** `gen_mesh.py` emits the
RV32 configuration; a card bitstream with this node is a separate step, and the
host window (§9) is a port on `sysnode` rather than a bus address.

## 2. The processor

`rv64_syscore`: **RV64IMA + Zicsr**, in-order, single issue, 5-stage, with a
branch predictor, a hardware multiplier and divider, and the `A` extension
(`LR`/`SC`, all AMOs, both widths). Toolchain flags:
`-march=rv64ima_zicsr -mabi=lp64 -mcmodel=medany`. No floating point, no
compressed instructions. Programs: `tests/rv64/hello.c`, `atomics.c`, `dhry.c`.

### 2.1 Privilege

**Machine, supervisor and user modes**, with delegation. Reset lands in
machine mode.

| ability | how | proven by |
|---|---|---|
| enter supervisor / user | `mret` with `MPP`; `sret` with `SPP` | `priv.c` |
| delegate exceptions / interrupts | `medeleg` (codes 0–15), `mideleg` (bits 1, 3, 5, 7, 9, 11) | `priv.c`, `sv39.c` |
| a privileged instruction below its level traps | `mret` outside M; `sret`, `sfence.vma` in U → cause 2 | `priv.c` |
| `ECALL` tells the caller's mode | cause 8 from U, 9 from S, 11 from M | `priv.c` |
| kernel reaches user pages | `mstatus.SUM` (loads/stores only — never fetch) | `osloop.c` |

### 2.2 CSRs

Unimplemented bits are **WARL zero**: they are not stored and read as 0.

| address | CSR | implemented |
|---|---|---|
| `0x100` `sstatus` | window on `mstatus` | SIE, SPIE, SPP, SUM, MXR |
| `0x104` / `0x144` `sie` / `sip` | windows on `mie` / `mip` | through `mideleg` |
| `0x105` `stvec`, `0x305` `mtvec` | direct mode only | bits 1:0 read 0; **non-zero installs a handler** |
| `0x140` `sscratch`, `0x340` `mscratch` | | 64 bits |
| `0x141` `sepc`, `0x341` `mepc` | | bit 0 reads 0 |
| `0x142` `scause`, `0x342` `mcause` | | bit 63 + a 5-bit code |
| `0x143` `stval`, `0x343` `mtval` | | 64 bits |
| `0x180` `satp` | | MODE (0 or 8) and a 28-bit PPN; no ASID |
| `0x300` `mstatus` | | SIE, MIE, SPIE, MPIE, SPP, MPP, SUM, MXR |
| `0x301` `misa` | read-only | RV64: A, I, M, S, U |
| `0x302` `medeleg`, `0x303` `mideleg` | | as above |
| `0x304` `mie`, `0x344` `mip` | | bits 1, 3, 5, 7, 9, 11; `mip[3]` writable |
| `0xB00`/`0xC00`, `0xB02`/`0xC02`, `0xC01` | `mcycle`, `minstret`, `time` | free-running; survive a halt |
| `0x7C0` `mtimecmp` | **non-standard** location | the timer compare |
| `0xF11`–`0xF14` | id registers | read 0 |

Any other address is an illegal instruction. `time` and `mtimecmp` compare on
one free-running 64-bit counter; the timer interrupt is the comparison itself
and is dismissed by moving `mtimecmp`.

### 2.3 Traps and interrupts

| cause | event | `tval` |
|---|---|---|
| 2 | illegal instruction, unimplemented CSR, privilege violation | 0 |
| 3 | `EBREAK` | 0 |
| 4 / 6 | misaligned load / store or AMO | the address |
| 8 / 9 / 11 | `ECALL` from U / S / M | 0 |
| 12 | instruction page fault | the PC |
| 13 / 15 | load / store page fault | the address |
| `1<<63` + 1 / 5 / 9 | supervisor software / timer / external interrupt | 0 |
| `1<<63` + 3 / 7 / 11 | machine software / timer / external interrupt | 0 |

Interrupt sources: **software** — the host doorbell register or `mip[3]`;
**timer** — `time >= mtimecmp`; **external** — a level raised by any of: a
mover descriptor that faulted, the host asking the node to stop, a completion
waiting in the dispatch mailbox (§8), or **a doorbell rung from another mesh**
(§7). An interrupt is deferred past a load, store or AMO and never interrupts a
multiply, divide or atomic that has started.

**No handler installed means halt.** With `mtvec` still zero an exception halts
the core and reports its cause to the host instead of jumping to address 0.

**Timing contract.** A trap or return moves the PC in the cycle it is taken;
`xepc`, `xcause`, `xtval`, the `mstatus` stack bits and the privilege level land
**one cycle later**, and instruction fetch is held for that cycle. Software
cannot observe the difference. Proven by every trap test above.

### 2.4 Virtual memory — Sv39

| ability | detail | proven by |
|---|---|---|
| three-level page tables walked in hardware | tables anywhere the node port reaches, typically staging | `sv39.c` |
| 32-entry direct-mapped TLB, `sfence.vma` sweeps it | superpages (2 MB, 1 GB) filled as 4 KB slices; a misaligned superpage PPN faults | `rv64_mmu` bench |
| data **and instruction fetch** translated | one shared MMU; fetch through a one-page register refilled on a page crossing | `sv39.c`, `osloop.c` |
| page faults are exceptions | 12 / 13 / 15 with `tval`, delegable | `sv39.c` |
| user code on its own pages, preempted by the timer | `link_sys.ld` places user text page-aligned (`.utext`) | `osloop.c` |

The card is **40-bit physical**: `satp.PPN` holds 28 bits. Supervisor may not
fetch from a `U` page (`SUM` does not relax fetch), so kernel and user text
never share a page.

### 2.5 The processor's address map

| region | address | size (shipped) | what |
|---|---|---|---|
| instruction window | `0x0000_0000` | 32 KB | fetch only; loaded by the host |
| scratchpad | `0x0001_0000` | 32 KB | byte-writable local memory; `.data`, `.bss`, stack |
| control region | `0x0002_0000` | 256 B | §5 |
| node, uncached | bit 39..28 set, **bit 31 clear** | 40-bit space | straight to the memory agent: staging, DRAM below 2 GB, other meshes' apertures |
| node, cached | bit 39..28 set, **bit 31 set** | 40-bit space | through the write-back L1 (2 KB, 64 lines) |

"Cached" is the single bit 31, not a magnitude compare. Staging apertures have
bit 39 set, so they are always uncached — which is what page tables and
mailboxes need.

## 3. Memory: what the processor can reach

| store | reach | width rules | proven by |
|---|---|---|---|
| scratchpad | load/store | any width | every program |
| the mesh's DRAM | load/store via the node port, cached or uncached by bit 31 | any width; the L1 writes back whole 32-byte lines | `sys_hello.c` |
| **staging** — the mesh's 2 MB on-chip store at aperture 0 | load/store, uncached | **any width: byte strobes are honoured**, so 8-byte page-table entries and mailbox words are safe | `ring_a.c`, `sv39.c` |
| another mesh's staging or DRAM | **not by load/store** — the processor's own port is local. Use the mover (§6) | — | `ring_a.c` |

Staging addresses: `{1'b1, 1'b0, mesh[37:36], aperture[35:32] = 0, offset[31:0]}`
— mesh 0's staging is `0x80_0000_0000`, mesh 1's `0x90_0000_0000`. The
address is global: the mesh field selects whose store.

## 4. The host window

A 32-bit-address, 64-bit-data port (`hs_*`) into the node. `hs_addr[31:28]`
selects the space.

| space | select | contents |
|---|---|---|
| instruction window | `0x0` | 32-bit words, byte offset |
| scratchpad | `0x1` | 64-bit words |
| control | `0x2` | the registers below, byte offset in `[7:0]` |

| offset | register | R/W |
|---|---|---|
| `0x00` | BOOT — write 1 to start the core at PC | W |
| `0x08` | PC — the entry point | W |
| `0x10` | DOORBELL — the host's software-interrupt line into the core | W |
| `0x18` | STATUS — `[3]` exited, `[2]` halted, `[1:0]` halt cause | R |
| `0x20` | EXIT — the word the program stored at exit | R |
| `0x28` | HALT PC | R |
| `0x30` / `0x38` | cycles / instructions retired since boot | R |

Halt causes: 0 external halt (a clean exit store, or the host), 1 `ECALL` with
no handler, 2 `EBREAK` with no handler, 3 illegal or misaligned with no
handler. The console is a byte stream on `hs_console`. Sequence: load image →
write PC → write BOOT → poll STATUS → read EXIT. Proven by every harness.

## 5. The control region — `0x0002_0000`

Word registers, 8-byte spaced. Reads answer a cycle later; writes take effect
the cycle after.

| offset | register | R/W | meaning |
|---|---|---|---|
| `0x00` | EXIT | W | **program exit is this store**, not `ECALL`: latches the word, halts the core with cause 0, sets STATUS.exited |
| `0x08` | CONSOLE | W | low byte to the host console |
| `0x10` | DOORBELL (host) | R | the host's line, bit 0 |
| `0x18` | SATP mirror | R | the CSR, read-only from here |
| `0x20` | MOVER STATUS | R | `[32]` busy, `[31:28]` fault code, `[27:0]` descriptors completed |
| `0x28` | DOORBELL COUNTS | R | inbound rings by source mesh: four 16-bit lanes, mesh 0 in `[15:0]` … mesh 3 in `[63:48]` |
| `0x40`–`0x78` | DISPATCH MAILBOX | RW | §8 |
| `0x80`–`0xB8` | MOVER CONFIG | W | §6 — `0x80 + register` |
| `0xC0`–`0xD0` | INTERLINK CONFIG | W | §7 — `0xC0 + register` |

## 6. The mover, from the processor

The mover moves 32-byte words between any addresses the memory agent reaches,
including **another mesh's staging**, by descriptor. The processor writes its
registers through the control region at `0x80 + register`; only registers
`0x00`–`0x38` are reachable from here (`0x40` immediate and `0x50` gather
pitch are host-only).

| register | fields | meaning |
|---|---|---|
| `0x00` | `[2:0]` mode, `[4:3]` element width, `[15:8]` flags, `[16]` go | writing with `go` set starts the descriptor |
| `0x10` | `[0]` sel (0 source, 1 destination), `[43:4]` base address, `[46:44]` ndim | a header |
| `0x18` | `[0]` sel, `[3:1]` dim, `[19:4]` count, `[51:20]` stride (bytes, signed) | one dimension |
| `0x20` | `[1:0]` axis, `[17:2]` axis step | the dimension's axis (0 for a plain copy) |
| `0x28` | `[0]` sel, `[1]` which, `[17:2]` bound, `[33:18]` extent | a bound axis (padding) |
| `0x30` | `[39:0]` index base, `[55:40]` index count | gather |
| `0x38` | seed | generate |

Modes: `0` COPY, `2` GATHER, `3` GENERATE, `4` FILL, `5` transform (the slot);
`1` transpose faults (the transform slot does it). Element width codes `0`/`1`/`2`
= 8/16/32-bit fill elements; `3` faults. Fault codes at STATUS `[31:28]`:
1 index length, 2 range, 3 AXI error, 4 mode, 5 element width, 6 alignment,
7 transform padding.

**A copy of N words, source to destination** (the sequence `ring_a.c` runs):

```
MV(0x10) = (1 << 44) | (src << 4) | 0;            // source header, 1 dim
MV(0x18) = (32 << 20) | (N << 4) | 0;             // dim 0: N words, stride 32
MV(0x20) = 0;
MV(0x10) = (1 << 44) | (dst << 4) | 1;            // destination header
MV(0x18) = (32 << 20) | (N << 4) | 1;
MV(0x20) = 0;
MV(0x00) = (1 << 16) | (1 << 3) | 0;              // go, COPY
while (MV_STAT & (1 << 32)) ;                     // busy
```

**Where a destination lands.** A write whose mesh field names another mesh
crosses the interlink. On arrival, a **special address (bit 39 — staging)
lands in that mesh's staging at the full 40-bit address**; a DRAM address lands
in that mesh's DRAM by its low 32 bits. Reads never cross: a source must be in
this mesh. Proven by `ring_a.c`/`ring_b.c` (mesh 0's mover fills mesh 1's
staging; mesh 1's processor reads it back).

## 7. The interlink and doorbells

Nodes chain **mesh 0 — mesh 1 — mesh 3 — mesh 2**, each with an up and a down
link; a packet for a farther mesh transits. What crosses: mover writes (§6),
compute-unit flits addressed to a remote memory node, and **doorbells**. What
does not: processor loads and stores, and any read.

The processor configures its own interlink at `0xC0 + register`:

| register | fields | meaning |
|---|---|---|
| `0xC0` | `[0]` enable, `[1]` clear the doorbell counts, `[2]` clear faults | enabled at reset |
| `0xC8` | `[1:0]` mesh id | defaults to the node's `MESH_ID` |
| `0xD0` | `[1:0]` destination mesh, `[15:8]` transaction tag | **writing rings that mesh** |

**Receiving.** Each inbound ring increments the count for its source mesh
(read at `0x28`), and **raises the external interrupt** while any count is
non-zero — a level, so a ring taken while another is being serviced is not
lost. The handler reads the counts, then clears them (`0xC0` bit 1); the level
drops with them. Proven by `ring_b.c` (mesh 1 services mesh 0's ring from its
interrupt handler) and `ring_a.c` (mesh 0 polls the count for the reply).

**The pattern for handing work to another mesh:** write the data into the far
mesh's staging with the mover, **wait for the mover to report idle**, then
ring. The ordering rests on two facts and needs both: the mover reports idle
only once every write packet has been accepted onto the link, which delivers
in order; and the receiving interlink holds an inbound doorbell until every
write that arrived ahead of it has been acknowledged by its memory. **The ring
is not a release fence on its own** — the sending arbiter rotates between
writes, flits and doorbells, so a ring issued while a burst is still leaving
can overtake it. Wait for idle first (`MV_STAT[32]` clear).

## 8. The dispatch mailbox — commanding compute units

At control offset `0x40`, 8-byte spaced:

| index | register | meaning |
|---|---|---|
| 0 | DST | `[3:0]` x, `[11:8]` y of the unit |
| 1, 2 | ARG0, ARG1 | two 64-bit payload words |
| 3 | GO | write 1: hardware builds a `CU_INST` flit and sends it |
| 4 | STAT | `[7:0]` completions queued, `[15]` a dispatch is still leaving, `[31]` **sticky overflow** |
| 5 | HEAD | the oldest completion: `[55:52]` src y, `[51:48]` src x, `[47:40]` code, `[39:8]` argument |
| 6 | POP | write 1 to drop the head |

Completions (`CU_SIGNAL` flits) queue 16 deep and raise the external interrupt
while the queue is non-empty; a 17th sets the overflow bit and is dropped.
Proven by `dispatch.c` against a modelled unit.

## 9. What the node does *not* do

- **No load/store to another mesh.** Cross-mesh data moves by the mover; the
  processor's port is local. Reads never cross the link.
- **No physical-address fault.** An address outside every region aliases or is
  dropped rather than trapping; the MMU faults only on translation.
- **No self-modifying code**, no `FENCE.I` semantics, no ASID, no PMP, no
  vectored trap entry, no debug module, no floating point.
- **The timer cannot be delegated** (no `stimecmp`): preemption is machine-mode
  work; a supervisor handles `ECALL`s and page faults.
- **Mover traffic is not translated**: descriptors carry physical addresses.
- **No isolation between requesters** on the card: a descriptor may name any
  memory.
- The transform slot's register port is not reachable from this processor.

## 10. The two-node system

`rv64_node_pair` (`src/kohakuaccel/verif/rv64_node_pair.v`,
`sim/verilator/harness/rv64_node_pair_main.cpp`) is two complete nodes on one
interlink with their own DRAM models, each running its own program. It is the
reference for everything in §6–§7 driven by the processors themselves:

```
python scripts/py/vlt.py rv64_node_pair --cc sim/verilator/harness/rv64_node_pair_main.cpp \
    --run-args "--elf-a ring_a.elf --elf-b ring_b.elf"
```

Programs for the node use `link_sys.ld` and **must be assembled with
`-DEXIT_ADDR=0x20000`**, so `crt0.S`'s exit store reaches the control region;
without it the exit word lands in the scratchpad and the host reads 0.

## 11. Verification behind this page

| bench / program | what it proves |
|---|---|
| `hello`, `atomics`, `csr` (bare core) | the ISA, atomics, CSR and timer traps |
| `priv` | M/S/U, delegation, illegal privileged instructions, misaligned causes |
| `rv64_mmu` bench | walks, TLB hits, permissions, superpages, machine passthrough, the shared port under pre-emption, fault ownership |
| `sys_hello` | node port, cached and uncached, L1 writeback |
| `sv39` | hardware-walked tables, a translated store read back physically, load and **instruction** page faults delegated to supervisor |
| `osloop` | user code under Sv39 preempted by the timer, resumed |
| `dispatch` | the mailbox and the completion interrupt |
| `ring_a` / `ring_b` on `rv64_node_pair` | strobed stores into staging, a mover copy into the far mesh's staging, the doorbell as an interrupt, the reply |
| `mag_mem_port`, `mag_wslot`, `mag_stage`, `mm_mesh`, `mm_mesh_stage`, `mm_mesh_peer`, `mag_1m_upload`, `interlink_stage`, `mm_prng`, `sysnode_ctrlpe` | the memory agent, staging, mover and interlink under host-driven traffic |

All under Verilator, `python scripts/py/vlt.py <bench> [--cc <harness>]`. Four
benches in the tree are xsim-only today and do not pass under Verilator on
any revision (`mag_link`, `mm_mover` §7, `interlink_2mesh_1m`,
`interlink_4mesh`); they are not evidence for this page.

## 12. What it costs — measured

Out-of-context synthesis, `xcvu13p-fhgb2104-2L-e`, Vivado 2024.2, one clock
at 3.333 ns, `scripts/tcl/ooc_sysnode_rv64.tcl 2` (`PORTS=2`, `STAGE=1`,
`ILINK=1`, `STAGE_AT_PORT=1`, 32 KB instruction window, 32 KB scratchpad,
64-line L1), design state Synthesized, reports `build/node_sn64_p2_*.rpt`.

| whole node, run of 2026-08-26 | LUT | FF | BRAM tiles | URAM | DSP | WNS |
|---|---:|---:|---:|---:|---:|---:|
| `sysnode`, RV64 complex | **32,859** | 46,436 | 57.5 | 65 | 47 | **+0.039 ns** |

The budget is 35,000 LUT, so the node is **2,141 under**. **300 MHz is met in
out-of-context synthesis**: WNS +0.039 ns at the 3.333 ns request, **0 failing
endpoints** of 124,100 — an achieved synthesis period of 3.294 ns. The last
cone to close was the mover's command-FIFO admission (`mode` → `fifo_room`'s
add-then-compare → `proc` → the write enable); registering that room limit
against a config-time constant took the add off the path.

**This is synthesis, not routing.** Elsewhere in this tree a module lost
0.740 ns from synthesis to routing — twenty times this margin — so the founded
claim is "meets 300 MHz in out-of-context synthesis," never "closed timing,"
and no Fmax above 300 MHz follows. There is no routed result and no silicon
measurement.

Inside the node, hierarchically, from the same run (`rebuilt` flow — module
totals exact, leaf attribution approximate; the three top-level rows sum to
the node, the three complex rows to the complex):

| instance | LUT | FF | DSP |
|---|---:|---:|---:|
| the RV64 complex (`rv64_mag_pe`) | 16,010 | 16,458 | 47 |
| — the processor (`rv64_syscore`) | 7,244 | 5,776 | 4 |
| — the mover (`mm_mover`) | 4,226 | 5,770 | 11 |
| — the transform slot (`mag_xform`) | 4,540 | 4,912 | 32 |
| the memory agent (`mag`) | 16,335 | 29,385 | 0 |
| — each memory port (`mag_mem_port`) | 2,064 / 2,032 | 4,847 | 0 |
| — the interlink switch and link (`mag_switch`, `mag_ilink`) | 2,435 / 1,294 | 3,736 / 2,214 | 0 |
| — the control agent (`noc_orchestrator`) | 2,240 | 2,546 | 0 |
| — the DRAM port (`mag_dram_port`) | 1,993 | 1,568 | 0 |
| the hub (`sn_hub`) | 514 | 581 | 0 |

`16,010 + 514 + 16,335 = 32,859`, and `7,244 + 4,226 + 4,540 = 16,010`.
