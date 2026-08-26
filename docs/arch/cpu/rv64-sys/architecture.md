---
title: RV64 system core architecture
summary: The contract — RV64IMA + Zicsr as implemented, the M/S/U privilege model with delegation, the two address spaces and their maps, the control registers, traps and interrupts and when their effects land, and the program-exit protocol.
tags:
  - architecture
  - cpu
  - rv64
---

# RV64 system core architecture

What software and the surrounding system may rely on. Everything on this page
is contract: an implementation may change anything else and nothing here
without a spec change. How it is built is
[microarchitecture](microarchitecture.md); what it costs is
[performance](performance.md).

The core is `rv64_core`. It presents one instruction port, one data port,
three interrupt-shaped inputs and a page-fault input on each memory port, and it
knows nothing about the fabric or MAG. **It does not translate**: it owns
`satp`, `priv`, `SUM` and `MXR` as architectural state and exports them, and the
wrapper around it holds the TLB and the page-table walker that use them
([memory-system](memory-system.md#sv39)). Everything below that is not the ISA
belongs to that wrapper — [integration](integration.md) says which wrapper.

## The instruction set

**RV64I + M + A + Zicsr**, in-order, single issue, with machine, supervisor and
user modes. Ordinary compilers work unmodified:
`-march=rv64ima_zicsr -mabi=lp64`.

| Group | Status |
|---|---|
| **RV64I** | complete, including the `W` forms (`ADDIW`, `ADDW`, `SLLW`, `SRLW`, `SRAW` and their immediate forms) |
| **RV64M** | complete: `MUL`, `MULH`, `MULHSU`, `MULHU`, `DIV`, `DIVU`, `REM`, `REMU`, and `MULW`, `DIVW`, `DIVUW`, `REMW`, `REMUW`. Multiply is 8 cycles, divide 66 |
| **RV64A** | complete for both widths: `LR`/`SC` and all nine `AMO` operations. `aq` and `rl` are **decoded but ignored** — see [ordering](#ordering) |
| **Zicsr** | `CSRRW`/`CSRRS`/`CSRRC` and the three immediate forms. `funct3 = 100` is illegal, as the specification requires |
| `FENCE`, `FENCE.I` | decoded, execute as **NOP** — one hart, in-order, one outstanding access, and the instruction window is not writable from the data side |
| `WFI` | decoded, executes as **NOP**. It does not idle the core |
| `MRET` | redirect to `mepc`; `priv ← MPP`, `MIE ← MPIE`, `MPIE ← 1`, `MPP ← U`. Illegal outside machine mode |
| `SRET` | redirect to `sepc`; `priv ← SPP`, `SIE ← SPIE`, `SPIE ← 1`, `SPP ← U`. Illegal in user mode |
| `SFENCE.VMA` | invalidates the **whole** TLB and the fetch page register. `rs1` and `rs2` are ignored — there are no ASIDs and one entry per index. Illegal in user mode |
| `ECALL`, `EBREAK` | trap if a handler is installed, otherwise halt — [below](#no-handler-installed-means-halt) |
| misaligned load, store or AMO | **faults**, cause 4 or 6. RV64 permits either fixup or fault |
| **`S`, `U` privilege** | **implemented**, with `medeleg`/`mideleg` delegation — [below](#the-privilege-model) |
| **`F`, `D`, `Zfh` — floating point** | **absent.** No `f0..f31`, no `fcsr`, no rounding mode |
| **`C` — compressed** | **absent.** Every instruction is 4 bytes |
| `Zifencei` semantics, PMP, `Zicntr` beyond three counters | **absent** — [below](#what-is-deliberately-absent) |

Two decode details are contract because software can observe them:

- **A shift amount is 6 bits at RV64 and 5 at the `W` forms**, and the bit above
  the field belongs to the operation, not the amount: `SRAI` differs from `SRLI`
  by `instr[30]` alone. `SLLIW`/`SRLIW`/`SRAIW` with `instr[25]` set are
  **illegal**, not shifts by 32 more.
- **`x0` is never a destination.** The decoder clears the write for `rd = 0`
  rather than the register file dropping it, so nothing in the pipeline believes
  a value was produced.

### Multi-cycle occupancy is architecturally visible

Not through a result — the ISA hides that — but through `mcycle` and through
interrupt latency. A multiply holds execute for 8 cycles, a divide for 66, an
atomic for 3 or 4, and **an interrupt cannot preempt one that has started**.
Worst-case interrupt latency is therefore bounded below by a divide.

## The privilege model

**Three levels — machine (M), supervisor (S) and user (U).** The current level
is a two-bit register inside `rv64_csr`, and reset lands in machine mode.

| `priv` | level | what it reaches |
|---|---|---|
| `3` | machine | every CSR, every instruction, and memory untranslated |
| `1` | supervisor | the `s*` CSRs, `SRET`, `SFENCE.VMA`, and memory through Sv39 when `satp` says so |
| `0` | user | no privileged CSR, no privileged instruction, and the same translation |

Supervisor mode exists for one concrete reason rather than for completeness: an
M+U machine can run user code under Sv39, but its kernel is untranslated and has
to walk the page tables in software to touch a user buffer. With S mode and
`mstatus.SUM` the kernel loads and stores a user page directly, which is what a
`copy_to_user` needs.

### How the level changes

- **A trap sets it.** A trap that is not delegated enters machine mode and
  writes `mepc`, `mcause`, `mtval` and the `mstatus` machine stack bits; a
  delegated one enters supervisor mode and writes the `s*` twins. Either way the
  level that was interrupted is recorded — in `MPP` or in `SPP` — so the return
  knows where to go.
- **`MRET` restores `priv` from `MPP`**, `MIE` from `MPIE`, sets `MPIE`, and
  leaves `MPP` at user. `SRET` does the same through `SPP`/`SPIE`/`SIE`.
- **Nothing else moves it.** There is no instruction that lowers privilege
  except a return, which is the architecture's rule and not this core's.

### What a level is checked against

Two checks, and both read the instruction encoding rather than a per-register
table, because the encoding is where RISC-V puts the answer.

- **A CSR access is checked on its address.** `addr[9:8]` is the level the CSR
  requires and `addr[11:10] == 11` marks it read-only, so a CSR named from too
  low a level, or a write to a read-only one, is an **illegal instruction**
  (cause 2). That is also how software discovers the set: every address the
  design does not implement is illegal too.
- **A privileged instruction below its level is illegal**, not a silent no-op:
  `MRET` outside machine mode, `SRET` or `SFENCE.VMA` in user mode. This is what
  stops user code returning to machine mode or flushing the TLB out from under
  the kernel.

### Delegation

`medeleg` and `mideleg` move a trap from machine mode to supervisor mode. A trap
is delegated when **the hart is running below machine mode** and the bit for its
cause is set; it then writes the `s*` registers, enters supervisor mode, and
vectors through `stvec`.

| register | bits stored | which are consulted |
|---|---|---|
| `medeleg` | exception codes **0..15** | the code of the exception being taken |
| `mideleg` | bits 1, 3, 5, 7, 9, 11 | **3, 7 and 11** — software, timer and external, the three positions `mip` raises |

`mideleg`'s odd low bits (1, 5, 9) are storable because they are the supervisor
half of the same six-bit window, but nothing reads them: `mip` never sets a
supervisor bit of its own, and a delegated interrupt is reported to the
supervisor with the supervisor cause code (1, 5 or 9) by the delegation itself.

**`ECALL`'s cause names the mode it came from** — 8 from user, 9 from
supervisor, 11 from machine — so one handler tells a user syscall from a
supervisor one without reading any other state.

**The timer cannot usefully be delegated.** `mtimecmp` is a machine CSR and
there is no `stimecmp`, so a supervisor handler handed a timer interrupt could
not dismiss it and would re-enter forever. Preemption is machine-mode work here;
the supervisor handles what it can finish, which is `ECALL` and page faults.

### The CSRs that exist

Only the ones the design names. Architecturally visible state is the expensive
part of a core, and a specification-complete CSR file would be most of one.
**Every address not in this table raises an illegal-instruction trap.**

Unimplemented bits are **not stored**. A write lands through a mask and reads
back as zero, which is what the architecture calls WARL and what keeps the file
small — the masks are part of the contract and are given here for that reason.

| Address | CSR | Implemented bits |
|---|---|---|
| `0x100` | `sstatus` | a **window** on `mstatus`, mask `0x000C_0122`: SIE, SPIE, SPP, SUM, MXR. Not a separate register |
| `0x104` | `sie` | a window on `mie` through `mideleg` |
| `0x105` | `stvec` | direct mode only; bits 1:0 read 0 |
| `0x140` | `sscratch` | 64 bits |
| `0x141` | `sepc` | bit 0 reads 0 |
| `0x142` | `scause` | bit 63 plus a 5-bit code |
| `0x143` | `stval` | 64 bits |
| `0x144` | `sip` | a window on `mip` through `mideleg` |
| `0x180` | `satp` | `MODE` 63:60 (0 or 8) and `PPN` 27:0 — 28 bits, because the card is 40-bit physical. **ASID is not implemented and reads 0** |
| `0x300` | `mstatus` | mask `0x000C_19AA`: SIE 1, MIE 3, SPIE 5, MPIE 7, SPP 8, MPP 12:11, SUM 18, MXR 19 |
| `0x301` | `misa` | read-only. `MXL = 2`, extensions **A, I, M, S, U** |
| `0x302` | `medeleg` | exception codes 0..15 |
| `0x303` | `mideleg` | bits 1, 3, 5, 7, 9, 11 |
| `0x304` | `mie` | bits 1, 3, 5, 7, 9, 11 — software, timer, external at each level |
| `0x305` | `mtvec` | direct mode only; bits 1:0 read 0. **Non-zero installs a handler** |
| `0x340` | `mscratch` | 64 bits |
| `0x341` | `mepc` | bit 0 reads 0. The PC of the trapping instruction |
| `0x342` | `mcause` | bit 63 plus a 5-bit code |
| `0x343` | `mtval` | see [what `tval` carries](#traps-and-interrupts) |
| `0x344` | `mip` | read-only except bit 3, which software may set and clear |
| `0xB00` / `0xC00` | `mcycle` / `cycle` | the same counter |
| `0xB02` / `0xC02` | `minstret` / `instret` | the same counter; an explicit write wins over the retire pulse in the same cycle |
| `0xC01` | `time` | the same free-running counter as `mtime` |
| **`0x7C0`** | **`mtimecmp`** | **non-standard.** RISC-V puts `mtimecmp` in a memory-mapped CLINT; this core places it in the machine custom CSR range |
| `0xF11`–`0xF14` | `mvendorid`, `marchid`, `mimpid`, `mhartid` | all read **0** |

**`sstatus`, `sie` and `sip` are windows, not copies.** A write through
`sstatus` leaves the machine-only bits of `mstatus` alone, and `sie`/`sip` show
and accept only what `mideleg` delegates. There is one register underneath each
pair, which is why a supervisor cannot lose track of what machine mode set.

**`mcycle`, `mtime` and `minstret` are free-running and nothing clears them.**
They keep counting across a halt. That is deliberate and it is the difference
from the RV32 PE, whose cycle counter resets on every kick and stops while
halted: a runtime that idles by halting must still be able to tell how long it
was idle.

**Reset clears control, not data.** `mstatus`, `mie`, `medeleg`, `mideleg`,
`satp`, the counters, `priv` and the two *vector installed* flags are reset;
`mtimecmp` resets to all-ones so the timer does not fire at boot. The trap
vectors, `xepc`, `xcause`, `xtval` and `xscratch` are **not** reset — they are
data written before they are read, and keeping 640 bits of register out of a
control set is what that buys. Software must not read any of them before a trap
or a write has given them a value.

### Traps and interrupts

A trap is taken **only at an instruction boundary**, which here means: the
instruction in execute is valid, the core is not halted, no memory access is
outstanding, and no multiply, divide or atomic is mid-sequence. A multi-cycle
operation that has started must finish, because its operands were latched on
entry and abandoning it would leave a transaction nobody completes.

| Cause | Raised by | `tval` |
|---|---|---|
| 2 | illegal instruction: a bad encoding, an unimplemented or too-privileged CSR address, a write to a read-only CSR, or a privileged instruction below its level | 0 |
| 3 | `EBREAK` | 0 |
| 4 | misaligned load | the effective address |
| 6 | misaligned store or AMO | the effective address |
| 8 / 9 / 11 | `ECALL` from user / supervisor / machine | 0 |
| **12** | **instruction page fault** — fetch translation failed | the faulting **PC** |
| **13** | **load page fault** | the effective address |
| **15** | **store or AMO page fault** | the effective address |
| `0x8000…0001` / `…0003` | software interrupt at supervisor / machine level | 0 |
| `0x8000…0005` / `…0007` | timer interrupt at supervisor / machine level | 0 |
| `0x8000…0009` / `…000B` | external interrupt at supervisor / machine level | 0 |

Which of the two cause codes an interrupt reports is decided by delegation: a
delegated interrupt is reported at supervisor level and vectors through `stvec`.

**Priority.** An **exception outranks an interrupt** in the same cycle — an
interrupt is considered only when no exception is raised — and the exceptions
themselves are ordered instruction fault, illegal, `EBREAK`, misaligned, data
page fault, `ECALL`. Among interrupts the order is external, then software, then
timer, which is the privileged specification's. An interrupt is additionally
**deferred past a load, store or AMO** rather than taken before it, which the
specification always permits.

*(The specification's usual rule is the other way round — an interrupt outranks a
synchronous exception. This core's order is stated here because software can
observe it.)*

**When an interrupt may be taken at all.** At privilege level *x*, an interrupt
destined for *x* is taken when the hart is running **below** *x*, or **at** *x*
with *x*'s global enable set in `mstatus`. Running above *x* never takes it.

Four properties are contract rather than detail:

1. **A trapping instruction retires nothing.** Its register writeback and its
   CSR write are both suppressed, because the handler re-executes it from
   `mepc`. A store cannot both write memory and trap: a misaligned store emits
   no byte strobes, and an illegal instruction is not a store.
2. **The timer interrupt has no acknowledge.** It is the comparison
   `mtime >= mtimecmp`, not a latch. A handler that does not move `mtimecmp`
   re-enters forever.
3. **Every interrupt is a level, and none is cleared by writing `mip`.** The
   software line reads as `mip` bit 3 together with the software-writable bit
   beside it, so a handler clears it at its source — the control-region doorbell
   register — not in `mip`.
4. **The external line is an OR of four sources**, and a handler has to
   establish which one raised it before it can clear it:

   | source | cleared by |
   |---|---|
   | a mover descriptor that faulted | clearing the fault at the mover |
   | the host asking the node to stop | the host |
   | a completion waiting in the dispatch mailbox | draining the queue ([integration](integration.md#the-dispatch-mailbox)) |
   | **a doorbell rung from another mesh** | clearing the inbound counts ([integration](integration.md#the-interlink-doorbell)) |

   The last two are what let a scheduler stop polling: work finishing on a
   compute unit and work arriving from a neighbouring mesh both wake it.

#### When a trap's effects land

This is a timing contract rather than a behaviour, and it is stated because it
is the one place where *when* differs from *what*:

> **In the cycle a trap or a return is taken, the core redirects the PC and
> nothing else. Every other effect — `xepc`, `xcause`, `xtval`, the `mstatus`
> stack bits and `priv` — lands one cycle later**, from registered copies.
> Instruction fetch is held for that one cycle.

**It is invisible to software, and that is the point.** The handler's first
instruction is at least two cycles behind the redirect, so there is no
instruction that can observe the intermediate state; a handler needs no delay
slot, no `nop`, and no re-read. Fetch is held because whether the *new* PC is
translated depends on `priv`, and `priv` has not landed yet — that is the only
consumer that would have seen the difference.

The reason it is built this way is frequency. The trap decision carries the
effective-address adder, through the misalignment test, and as the clock enable
of roughly two hundred CSR flip-flops it was the whole node's critical path.
Registering the data and letting only the redirect stay combinational is one of
four changes to that path; together they took the node's worst slack from
−1.371 ns to −0.081 ns and left the only failing cone in the node outside this
processor. That cone has since been closed too, so **the node now meets its
300 MHz request in out-of-context synthesis with nothing failing** — which is
not the same as closed timing, and
[performance](performance.md#timing-the-node-meets-300-mhz-in-synthesis) says
why.

One consequence is visible in a counter rather than in control flow: **`retire`,
and so `minstret`, is a registered pulse and is one cycle late.** A count one
cycle late is still a count.

### No handler installed means halt

A trap vector still zero is a program that never installed a handler, and
jumping to address 0 would silently restart it. So an exception with no vector
installed **halts the core and reports a cause** instead of trapping. Once the
vector is non-zero, exceptions and interrupts trap normally.

Two details follow from delegation and from how *installed* is tracked:

- **The vector that has to be installed is the one the trap would use.** A
  delegated trap needs `stvec`; an undelegated one needs `mtvec`.
- **Installed is a property of the write, not of the value read back.** Writing
  a vector a non-zero value sets a flag, and the flag is what the trap decision
  tests. The vectors themselves are not reset, so testing them directly would
  mean reading a register that has never been written.

An **interrupt** with no vector installed is simply not taken; only an exception
halts.

| Halt cause | Raised by | `halt_pc` |
|---|---|---|
| 0 | the external halt input — a control-region exit store, or the host | the PC in execute |
| 1 | `ECALL` with no handler | the `ECALL`'s PC |
| 2 | `EBREAK` with no handler | the `EBREAK`'s PC |
| 3 | illegal encoding or misaligned access, with no handler | the offending PC |

A halt stops fetch, decode, execute **and** writeback. It is not a trap: nothing
is saved and there is no way to resume except a reset.

## The two address spaces

The core issues 64-bit addresses. What they mean is the wrapper's, and the two
wrappers answer differently. Neither wrapper faults on an unmapped **physical**
address — see [what is deliberately absent](#what-is-deliberately-absent). An
unmapped **virtual** address does fault, with cause 12, 13 or 15, whenever
translation is on ([memory-system](memory-system.md#sv39)).

### As a mesh compute unit — `rv64_sys_pe`

Harvard and local. **A load or store reaches the scratchpad or the control
region and nothing else**; there is no path off the unit.

| Region | Base | Size (default) | Semantics |
|---|---|---|---|
| instruction window | `0x0000_0000` | `IMEM_WORDS × 4 B` — 16 KB | **fetch only.** Not writable by the core and not readable from the data side |
| scratchpad | `0x0001_0000` | `SPAD_WORDS × 8 B` — 16 KB | ordinary read/write memory, byte-writable, one cycle |
| control region | `0x0002_0000` | 256 B | word registers, some with side effects |

`.rodata` is read with loads, so it must be linked into the scratchpad, not
beside `.text` — [programming](programming.md#the-link-maps).

### As the node's processor — `rv64_syscore`

The same three local regions, larger, plus the whole card address space out the
node port. The card is a **40-bit** machine; the map above 4 GB and the aperture
bit are [address-map](../../../address-map.md)'s.

**The tests below are on the physical address**, which is the address the core
issued only while translation is off. With Sv39 on, both fetch and data are
translated first and the decode sees the result — so a page table decides which
of these regions a virtual address lands in.

| Region | Test on the physical address | Semantics |
|---|---|---|
| instruction window | fetch, `IMEM_WORDS × 4 B` — 32 KB | fetch only |
| scratchpad | `pa[39:15] == 2` — 32 KB at `0x0001_0000` | ordinary read/write memory, byte-writable |
| control region | `pa[39:8] == 0x200` — 256 B at `0x0002_0000` | word registers, some with side effects |
| node, **uncached** | any of `pa[39:28]` set, and `pa[31]` **clear** | straight to the node port: staging, node registers, cross-mesh |
| node, **cached** | any of `pa[39:28]` set, and `pa[31]` **set** | through the write-back L1 |

**Read the cached test literally.** It is `pa[31]`, a single bit, not
"at or above 2 GB" — the decode is bit tests rather than magnitude compares
because it sits in the pipeline's stall path, and a 40-bit comparison there cost
frequency across the whole core. An address at 4 GB with bit 31 clear is
therefore **uncached**, and so is anything in the aperture. Lay a program's
cached working set out accordingly; [memory-system](memory-system.md#what-is-cached-and-what-is-not)
carries the consequences.

Nothing is linked into the node range. There is no image to place there, and
the loader does not write it — it is reached through pointers.

## What is deliberately absent

- **No unmapped-address fault.** Neither wrapper faults on an address outside
  its map. In `rv64_sys_pe` a store outside the scratchpad and control region is
  **dropped** and a load outside them **aliases onto the scratchpad**, because
  the scratchpad's index is the low address bits and the return path defaults to
  it. In `rv64_syscore` the same is true of the region below the node base. The
  core faults on a misaligned access and on an illegal encoding; it does not
  fault on a region.
- **No self-modifying code.** The instruction window has no write port the core
  can reach, in either configuration. `FENCE.I` is a NOP and cannot be made
  meaningful.
- **No `mstatus.MPRV`.** `SUM` and `MXR` are implemented and reach the MMU;
  `MPRV` — machine mode borrowing the previous level's translation — is not.
- **No PMP and no physical memory protection of any kind.** Isolation between a
  runtime and what it runs is Sv39's page tables and nothing else.
- **No ASID.** `satp.ASID` reads zero and `SFENCE.VMA` sweeps the whole TLB, so
  an address-space switch costs a full refill rather than a tagged one.
- **No `stimecmp`, and so no delegable timer** —
  [above](#delegation).
- **No vectored trap entry.** `mtvec` and `stvec` are direct mode only; their
  two-bit `MODE` field reads zero and a vectored base is not vectored. Write a
  4-byte-aligned address.
- **No performance counters beyond `mcycle` and `minstret`.** No `mhpmcounter`
  set, no event selectors.
- **No debug module.** `EBREAK` keeps its architectural cause and there is
  nothing to attach to.
- **No `A` extension ordering bits.** `aq` and `rl` decode and are discarded.
  This is safe rather than sloppy: one hart, in-order issue, and one outstanding
  memory access mean every access is already globally ordered with respect to
  every other — the guarantee is stronger than any `aq`/`rl` pair asks for. It
  stops being safe the moment a second hart or a non-blocking cache exists.

### Ordering

What the core guarantees to whatever waits on it — including the ordering
obligation it inherited by not having a compute-unit shell — is
[memory-system](memory-system.md#what-the-core-publishes-about-ordering), because
every rule in it is a property of the memory path rather than of the pipeline.

## Program exit is a store

**The terminator is a store to the control region, not `ECALL`.**

`ECALL` has to remain a call — that is the point of having a trap model at all —
and the framework's halt-and-report completion cannot move, so the terminator
moved instead. The core carries an external halt input for it, and a
store-driven exit reports **cause 0**: a clean finish, not a fault.

| | mesh compute unit | node processor |
|---|---|---|
| the store | `CTRL_BASE + 0x00`, 32 bits kept | `CTRL_BASE + 0x00`, 64 bits kept |
| what it does | latches the exit word, halts the core, and the shell sends a `CU_SIGNAL` carrying it | latches the exit word, halts the core, and sets `exited` in the host status register |
| the completion's fault flag | set when the halt cause is 2 or 3 — `EBREAK` or a fault | not applicable; the host reads cause and PC directly |

If `EBREAK` were the exit, every clean finish would report as a fault, because
`EBREAK`'s cause is a debug cause and the shell maps causes 2 and 3 to
`exec_fault`. Keeping the two separate is why the store exists.

The exit word's meaning is software's. `crt0.S` puts `main`'s return value
there, and the convention that zero means success is the test suite's, not the
hardware's.

### `exited` is the success signal, not the halt cause

The halt the exit store raises is registered, so the instruction behind the
store can still reach execute — and in `crt0.S` that instruction is a trailing
`ECALL`. With no handler installed it halts the core in its own right, and the
halt cause the host reads is then **1**, not 0.

> **Read `exited` and the exit word. Treat the halt cause as meaningful only
> when `exited` is clear.** A halt cause of 1 beside a set `exited` is the
> start-up code's trailing `ECALL` retiring, not a failure.

The `ECALL` is not vestigial: it is what stops a program whose exit store went
somewhere harmless — the wrong `EXIT_ADDR`, most often
([programming](programming.md#entry-and-exit)) — from running into whatever
follows it in memory.
