---
title: Programming the RV64 system core
summary: The three link maps and when each applies, the control region as code including the dispatch mailbox, how to build a program, how to start one, how to write a trap handler across three privilege levels, and the rules software has to obey.
tags:
  - architecture
  - cpu
  - rv64
  - programming
---

# Programming the RV64 system core

Bare metal, no library, no operating system underneath. A program is `.text`,
`.rodata`, `.data`, `.bss` and a stack; a small assembly entry sets the stack,
clears `.bss`, calls `main`, and reports the result through a store.

Read [architecture](architecture.md) for what the hardware promises. This page is
how to write against it.

## First decide which map you are building for

The three differ in where memory *is*, and building for the wrong one produces a
program that links cleanly and loads nothing useful.

| target | link script | memory | reached over |
|---|---|---|---|
| the standalone Verilator harness | `tests/rv64/link.ld` | **flat** — one 64 MB region, everything at 0 | an ELF loader in the harness |
| the mesh compute unit, `rv64_sys_pe` | `tests/rv64/link_pe.ld` | **Harvard** — 16 KB instruction window, 16 KB scratchpad | `CU_DATA` flits |
| the node processor, `rv64_syscore` | `tests/rv64/link_sys.ld` | **Harvard** — 32 KB instruction window, 32 KB scratchpad, plus the whole card | host AXI writes |

### The link maps

`link.ld` is flat because the harness's memory model is sparse and answers any
address. There is no read-only/read-write split to honour, so `.rodata` sits
beside `.text`.

The two node maps are **not** flat, and the reason is structural: instruction
fetch reaches the instruction window and every load and store reaches the
scratchpad, so **`.rodata` must be linked into the scratchpad**. It is read with
loads, and a load never reaches the instruction window.

```
   IMEM  ORIGIN 0x00000000     .text only
   SPAD  ORIGIN 0x00010000     .rodata, .data, .bss, then the stack
```

`.text` is aligned to 32 bytes at its end, because the loader writes whole
256-bit granules.

> **The link script's sizes must match the module's parameters.** `link_pe.ld`
> is written for `IMEM_WORDS = 4096` and `SPAD_WORDS = 2048`; `link_sys.ld` for
> 8192 and 4096. Changing one without the other **silently truncates the image**
> at the loader's bounds check — the link succeeds, the load succeeds, and the
> program runs off the end of what arrived.

The stack is placed above `.bss` by the script (`__stack_top`): 4 KB on the mesh
unit, 8 KB on the node processor, 32 KB in the flat harness. There is no guard
page and no overflow detection — the scratchpad wraps.

### User-mode code needs its own text pages

`link_sys.ld` carries a fourth output section, **`.utext`, aligned to 4 KB at
both ends**, and it is there for a hardware rule rather than for tidiness:

> **Supervisor may not fetch from a page marked `U`.** `mstatus.SUM` relaxes
> loads and stores and never instruction fetch, so kernel text and user text
> cannot share a page.

Aligning the user function alone does not solve it, because the linker packs the
next kernel function in behind it and that function then lives on a `U` page the
kernel cannot execute. Put every routine that runs in user mode into `.utext`:

```c
__attribute__((section(".utext"), aligned(4), naked))
void u_loop(void) { /* ... */ }
```

The section is empty in programs that have no user mode, and costs one page of
instruction window when it is not. Map its pages `V|R|X|A|U` and the rest of
`.text` `V|R|X|A` — [memory-system](memory-system.md#supervisor-may-not-fetch-from-a-user-page).

## Entry and exit

`tests/rv64/crt0.S` is the whole runtime:

1. zero `x1` through `x31`, because the core comes out of reset with every
   register undefined and leaving one undefined makes a bug depend on the
   simulator;
2. load `sp` from `__stack_top`;
3. clear `.bss` eight bytes at a time — the link script aligns both ends;
4. `call main`;
5. store `main`'s return value to `EXIT_ADDR`;
6. `ecall`, then loop, as belt and braces.

**The exit is the store, not the `ecall`.** `EXIT_ADDR` is a define, and it has
to match the target:

| target | `EXIT_ADDR` |
|---|---|
| the flat harness | `0x10000008` — the default in `crt0.S` |
| either node configuration | `0x20000` — `CTRL_BASE + 0x00` |

> **A node program must be built with `-DEXIT_ADDR=0x20000`.** The default is
> the bare core's address, which on a node target is an ordinary scratchpad
> word: the store succeeds, nothing halts, and **the host reads an exit word of
> 0** — a pass and a silent failure look identical.

**A halt reported after the exit store is the `ecall`, and it is not a
failure.** The exit store latches the word and sets `exited`, but the halt it
raises is registered, so `crt0.S`'s trailing `ecall` can still reach execute;
with no handler installed that `ecall` halts the core in its own right and
reports **halt cause 1**. So:

> **`exited` is the success signal, not the halt cause.** Read `STATUS.exited`
> and `EXIT`. A halt cause of 1 beside `exited` is the trailing `ecall`, not a
> fault; treat the halt cause as meaningful only when `exited` is clear.

That `ecall` is there for the case the define was wrong: a program whose exit
store went to a scratchpad word still stops, with a cause that says something
happened, instead of running into whatever follows.

## Building

```
riscv64-unknown-elf-gcc -march=rv64ima_zicsr -mabi=lp64 -mcmodel=medany \
  -nostdlib -nostartfiles -ffreestanding -O2 \
  -T tests/rv64/link.ld tests/rv64/crt0.S prog.c -o build/rv64/prog.elf -lgcc
```

| flag | why |
|---|---|
| `-march=rv64ima_zicsr` | `zicsr` is required for any CSR access — without it the assembler rejects `csrr`. Drop `m` and `a` to test the soft-routine paths; the core implements both |
| `-mabi=lp64` | the soft-float ABI. There is no float unit and no float ABI to choose |
| `-mcmodel=medany` | position-independent-ish addressing over a 2 GB window; `medlow` assumes the low 2 GB and breaks on any node address |
| `-nostdlib -nostartfiles -ffreestanding` | there is no C library and no `_start` but the one in `crt0.S` |
| `-lgcc` | the soft multiply, divide and 128-bit helpers. Keep it even at `rv64ima` |

**For either node target, both the exit address and the link script are
required** — neither is optional, and omitting either one produces a program
that builds and loads and reports nothing:

```
   mesh compute unit:  -DEXIT_ADDR=0x20000 -T tests/rv64/link_pe.ld
   node processor:     -DEXIT_ADDR=0x20000 -T tests/rv64/link_sys.ld
```

| omitted | what happens |
|---|---|
| `-DEXIT_ADDR=0x20000` | `crt0.S` keeps the bare core's default, the exit word lands in the scratchpad, and the host reads **0** |
| `-T tests/rv64/link_sys.ld` | the flat map puts `.rodata` beside `.text`, where a load cannot reach it |

## The control region

256 bytes at `0x0002_0000` in both node configurations. Reads are one cycle,
writes take effect the cycle after the access completes.

### On the mesh compute unit

```c
#define CTRL_BASE 0x00020000UL
#define R_EXIT    ((volatile unsigned long *)(CTRL_BASE + 0x00))
#define R_CONSOLE ((volatile unsigned long *)(CTRL_BASE + 0x08))
#define R_DBELL   ((volatile unsigned long *)(CTRL_BASE + 0x10))

static void putch(char c) { *R_CONSOLE = (unsigned char)c; }
```

| offset | write | read |
|---|---|---|
| `0x00` | end the run; the low 32 bits become the completion's argument | the doorbell bit |
| `0x08` | one console byte, observation only | the doorbell bit |
| `0x10` | the doorbell; bit 0 reaches the software interrupt line | the doorbell bit |

**Every read of this region returns the doorbell bit**, whatever the offset —
the read is a registered single value, not a decoded one. Do not read back the
exit word or the console.

### On the node processor

```c
#define CTRL_BASE 0x00020000UL
#define R_EXIT    ((volatile unsigned long *)(CTRL_BASE + 0x00))
#define R_CONSOLE ((volatile unsigned long *)(CTRL_BASE + 0x08))
#define R_DBELL   ((volatile unsigned long *)(CTRL_BASE + 0x10))
#define R_SATP    ((volatile unsigned long *)(CTRL_BASE + 0x18))
#define R_MVSTAT  ((volatile unsigned long *)(CTRL_BASE + 0x20))
#define R_DBCNT   ((volatile unsigned long *)(CTRL_BASE + 0x28))
#define NM(i)     ((volatile unsigned long *)(CTRL_BASE + 0x40 + (i)*8))
#define MV_CFG(i) ((volatile unsigned long *)(CTRL_BASE + 0x80 + (i)*8))
#define IL_CFG(i) ((volatile unsigned long *)(CTRL_BASE + 0xC0 + (i)*8))
```

| offset | write | read |
|---|---|---|
| `0x00` | end the run; the full 64-bit word is kept | 0 |
| `0x08` | one console byte | 0 |
| `0x10` | **nothing** — this doorbell is the host's to write, through its own window | the host doorbell bit |
| `0x18` | **nothing** — `satp` is CSR `0x180` and this is a read-only mirror of it | `satp` |
| `0x20` | — | mover status: `[32]` busy, `[31:28]` fault code, `[27:0]` descriptors completed |
| `0x28` | — | **inbound doorbell counts**, four 16-bit lanes: mesh 0 in `[15:0]` … mesh 3 in `[63:48]` |
| `0x40`–`0x7F` | the **dispatch mailbox**, one register per 8-byte slot | the mailbox |
| `0x80`–`0xBF` | one **mover config** register per 8-byte slot | 0 |
| `0xC0`–`0xFF` | the **interlink config** window, one register per 8-byte slot | 0 |

**A mover command is a sequence of ordinary stores.** `mv.go` is not an opcode:
the descriptor is data a program builds in its own memory, and issuing it is
stores into the config window in program order, which *is* the queue. The
register index is the low six bits of the offset, so `MV_CFG(i)` writes config
register `i * 8`. Poll `R_MVSTAT` for completion; the mover's own register
layout and command set are [sysnode](../../sysnode/README.md)'s.

**`satp` is a CSR, and this word is a read-only mirror for the host.** Write it
with `csrw satp`; storing to `CTRL_BASE + 0x18` changes nothing. Two writable
copies of a translation root is one too many
([memory-system](memory-system.md#satp-is-a-csr-and-the-control-region-mirrors-it)).

### Dispatching work to a compute unit

The window at `0x40` is `rv64_noc_mbox`, and it is how a program on this
processor commands the mesh. **Software writes a dispatch, not a flit**: a flit
is 288 bits against a 64-bit store port, so composing one in software would be
five stores with a tearing window in the middle. Hardware assembles it.

| index | offset | register | |
|---|---|---|---|
| 0 | `0x40` | `DST` | the destination's mesh coordinates: **x in bits 3:0, y in bits 11:8** |
| 1 | `0x48` | `ARG0` | payload word 0 |
| 2 | `0x50` | `ARG1` | payload word 1 |
| 3 | `0x58` | `GO` | any write builds a `CU_INST` flit from `DST`, `ARG0`, `ARG1` and sends it |
| 4 | `0x60` | `STAT` | read: `[4:0]` completions queued, `[15]` a dispatch still waiting to leave, `[31]` **sticky overflow** |
| 5 | `0x68` | `HEAD` | read: the oldest completion, or 0 if the queue is empty |
| 6 | `0x70` | `POP` | any write drops the head |

A completion arrives as one 64-bit word:

```
   [55:52] src_y   [51:48] src_x   [47:40] code   [39:8] arg
```

```c
*NM(0) = (1UL << 8) | 2UL;    /* the unit at y = 1, x = 2 */
*NM(1) = payload_lo;
*NM(2) = payload_hi;
*NM(3) = 1UL;                 /* GO — the flit is built and sent */

/* ... a completion raises the external interrupt ... */

unsigned long head = *NM(5);
unsigned long arg  = (head >> 8)  & 0xffffffffUL;
unsigned long code = (head >> 40) & 0xffUL;
*NM(6) = 1UL;                 /* POP */
```

Five rules, and each of them is something the hardware does that software has to
match:

1. **Popping is a write, not a side effect of the read.** The control region
   answers a read from a register a cycle later, so a read-triggered pop would
   have to guess which cycle the read really happened on. Read `HEAD`, then
   write `POP`.
2. **A completion raises the external interrupt line**, beside the node's own
   summary. A scheduler does not have to poll. The line stays asserted while the
   queue is non-empty, so a handler that does not drain the queue must mask
   `mie[11]` or it re-enters forever — draining is the scheduler's job, not the
   handler's.
3. **The queue is 16 deep and overflow is sticky, not blocking.** A flit the
   queue cannot take is still accepted and dropped — held, it would sit at the
   head of the hub's queue and stall the link for everything behind it,
   including the traffic that would drain it. Check `STAT` bit 31: **a dropped
   completion and a unit that never finished look identical otherwise.**
4. **A dispatch is held until the fabric takes it.** Writing `GO` while a
   previous flit is still waiting does nothing; `STAT` bit 15 says so. Withdrawing
   an offered flit would destroy it silently.
5. **`DST`, `ARG0` and `ARG1` persist.** Dispatching again to the same unit with
   the same payload is one write to `GO`.

The complex answers at mesh coordinate **(0,0)**, which is a corner, so it costs
no attachment point — [integration](integration.md#the-dispatch-mailbox).

### Ringing another mesh, and being rung

The window at `0xC0` is the processor's own reach into the interlink. Three
registers:

| index | offset | fields |
|---|---|---|
| 0 | `0xC0` | `[0]` enable (set at reset), `[1]` **clear the inbound counts**, `[2]` clear faults |
| 1 | `0xC8` | `[1:0]` this node's mesh id — defaults to its `MESH_ID` |
| 2 | `0xD0` | `[1:0]` destination mesh, `[15:8]` transaction tag — **writing this rings that mesh** |

```c
*IL_CFG(2) = (tag << 8) | dst_mesh;    /* ring mesh `dst_mesh` */
```

Receiving is an interrupt, not a poll. Each inbound ring increments the count
for its source mesh at `R_DBCNT`, and **any non-zero count holds the external
interrupt line up** — a level, so a ring that arrives while another is being
serviced is not lost:

```c
unsigned long counts = *R_DBCNT;       /* mesh n in bits [16n+15 : 16n] */
/* ... service them ... */
*IL_CFG(0) = (1UL << 1) | 1UL;         /* clear the counts, keep enable */
```

The level drops when the counts do. A handler that reads the counts without
clearing them re-enters forever, the same trap as the timer and the completion
queue.

**Wait for the mover before you ring.** The ring is **not** a release fence on
its own: the sending arbiter rotates between writes, flits and doorbells, so a
ring issued while a burst is still leaving can overtake it and land before the
data it announces.

```c
/* ... build and start the descriptor ... */
while (*R_MVSTAT & (1UL << 32)) { }    /* MV_STAT[32] — the mover is busy */
*IL_CFG(2) = (tag << 8) | dst_mesh;    /* only now is the ring safe */
```

With the wait in place the handoff is sound, and it takes two facts rather than
one: the mover reports idle only once every write packet has been accepted onto
the link, and the link delivers in order; and the *receiving* interlink holds an
inbound doorbell until every write that arrived ahead of it has been
acknowledged by its memory. The second is free; the first is the loop above.
**Write, wait for idle, ring** —
[integration](integration.md#the-interlink-doorbell).

**This is a different doorbell from the one at `0x10`.** That one is the
*host's* line into this core and arrives as a **software** interrupt; this one
comes from another mesh and arrives as an **external** interrupt. They share no
register and are cleared in different places.

## Starting a program

### The mesh compute unit

From the fabric, the ordinary sequence for any compute unit:

1. write the image with `CU_DATA` flits — `buf_id` 1 for instruction-window
   granules, 0 for scratchpad granules, 5 and 4 for single words;
2. send a `CU_INST` with **opcode 1**. Any other opcode retires immediately
   without running anything;
3. wait for `CU_SIGNAL`. Its argument is the exit word; its fault flag is set if
   the halt cause was 2 or 3 — `EBREAK` or a fault.

**The kick cannot overtake the image.** `CU_INST` and `CU_DATA` arrive on two
queues, and the unit holds a kick until its receive path is quiet, so an image
still arriving when the kick lands is finished first.

**The kick's start PC and argument word are ignored.** A program always begins
at address 0 and receives nothing. Pass an argument by writing it into the
scratchpad before the kick.

### The node processor

From the host, over the AXI slave window (`hs_addr[31:28]` selects):

1. selector **0** — write `.text` as 32-bit words;
2. selector **1** — write `.rodata` and `.data` as 64-bit words with byte
   strobes;
3. selector **2**, offset `0x00` — write 1 to `HR_BOOT`;
4. poll offset `0x18`, `HR_STATUS` — `{exited, halted, cause[1:0]}`;
5. read `HR_EXIT`, `HR_HALTPC`, `HR_CYCLES`, `HR_RETIRED`.

`HR_PC` at offset `0x08` exists and is **not used** — the reset PC is fixed at 0.
The memory selectors are **write-only**: reading them returns control-register
values, not the image.

The halt state is latched, so `HR_STATUS`, `HR_EXIT` and `HR_HALTPC` stay
readable after the core has gone back into reset.

## Rules software has to obey

**Alignment is enforced.** A misaligned load, store or AMO **faults** — cause 4
or 6, with the faulting address in `mtval`. There is no fixup. `-O2` will not
produce one from correct C; a hand-written cast that misaligns will.

**Anything another agent reads must be uncached.** On the node processor, the
cached range is `pa[31]` set within the node range, and it is for this core's own
working set. Descriptors a mover will fetch, doorbells, mailbox words and
completion flags all go through the **uncached** range, which is what the
aperture and the low node range are. There is **no cache flush and no
invalidate** available to software, so a cached write may never reach memory —
[memory-system](memory-system.md#what-the-core-publishes-about-ordering).

**`FENCE` is a NOP and cannot be made to mean anything.** One hart, in-order,
one outstanding access. `FENCE.I` likewise, and self-modifying code is
impossible in either configuration — the instruction window has no write port
the core can reach.

**An unmapped *physical* address does not fault.** It aliases onto the
scratchpad on a load and is dropped on a store. A wild pointer in machine mode
is silent. An unmapped **virtual** address does fault — cause 12, 13 or 15 —
whenever Sv39 is on, which is the only memory protection this machine has.

**No floating point.** No `f` registers, no `fcsr`. `printf`-style float
formatting, `libm`, and anything that spills a float will not link, and should
not be made to.

**Multiply is 8 cycles and divide is 66**, and both stall the whole pipeline.
Keep divides out of inner loops; a divide by a constant strength-reduces to
`mulhu` and covers the case a scheduler actually meets — turning a linear index
into coordinates.

### Writing a trap handler

```c
csrw("mtvec", (unsigned long)trap_entry);   /* 4-byte aligned, and non-zero */
csrs("mie", (1UL << 7) | (1UL << 3));       /* timer, software */
csrs("mstatus", (1UL << 3));                /* MIE */
```

Five things the handler has to get right, and every one of them is a property of
this core rather than of RISC-V in general:

1. **Install `mtvec` before anything can fault.** With `mtvec` still zero an
   exception **halts the core and reports a cause** instead of trapping
   ([architecture](architecture.md#no-handler-installed-means-halt)). That is
   the bare-metal behaviour the test programs rely on, and it is why `mtvec`
   being zero is not a valid handler address.
2. **`mtvec` is taken whole.** The two-bit `MODE` field is not decoded, so write
   a 4-byte-aligned base and do not set the vectored bit.
3. **`mepc` points at the trapping instruction.** For an *exception* — `ECALL`
   included — the handler must add 4 before `MRET`, or the instruction
   re-executes forever. For an *interrupt*, resume on it unchanged.
4. **The timer has no acknowledge.** Move `mtimecmp` (CSR `0x7C0`) in the
   handler or it re-enters forever. It is the comparison `mtime >= mtimecmp`,
   not a latch.
5. **Clear every interrupt at its source.** Not one of them is an edge, and
   none is cleared by writing `mip`:

   | line | source | cleared by |
   |---|---|---|
   | software | the host's doorbell register at `0x10` | the host, or `mip[3]` for the software-writable bit beside it |
   | timer | `time >= mtimecmp` | moving `mtimecmp` |
   | external | a mover fault, a host halt request, a queued completion, **an inbound interlink ring** | clearing the mover fault, draining the mailbox with `POP`, clearing the counts at `0xC0` bit 1 |

   The external line is an **OR of four levels**, so a handler must establish
   *which* raised it — read the mover status, the mailbox `STAT` and the
   doorbell counts — and clear that one. Clearing none of them and returning
   re-enters forever; masking `mie[11]` is the escape while the scheduler
   catches up.

There are no shadow registers. `mscratch` is the only scratch CSR; the handler
saves what it clobbers, and its prologue runs on the interrupted stack.

**Nothing in a handler has to account for when the trap's state landed.** The
core redirects the PC in the trap cycle and writes `mepc`, `mcause`, `mtval`,
the `mstatus` stack bits and the privilege level one cycle later, holding fetch
in between — so by the time the handler's first instruction executes, all of it
is in place. No delay slot, no `nop`, no re-read
([architecture](architecture.md#when-a-traps-effects-land)).

### Running below machine mode

Dropping to supervisor or user mode is a return, not an instruction of its own:
set the stack bits and `xepc`, then `mret` or `sret`.

```c
unsigned long ms = csrr("mstatus");
ms = (ms & ~(3UL << 11)) | (1UL << 11);   /* MPP = supervisor */
csrw("mstatus", ms);
csrw("mepc", (unsigned long)&s_entry);
__asm__ volatile ("mret");                 /* now in S, at s_entry */
```

From supervisor, clear `SPP` and `sret` to reach user mode. Four things decide
whether that works:

1. **Install `stvec` too.** A delegated trap vectors through `stvec`, and a
   delegated trap with `stvec` still zero halts the core exactly as an
   undelegated one with `mtvec` zero does.
2. **Delegate deliberately, one cause at a time.** `medeleg` bit *n* delegates
   exception cause *n*; `mideleg` bits 3, 7 and 11 delegate the software, timer
   and external interrupts. The bits a runtime under Sv39 usually wants are

   ```c
   csrw("medeleg", (1UL << 8)     /* ECALL from user       */
                 | (1UL << 12)    /* instruction page fault*/
                 | (1UL << 13)    /* load page fault       */
                 | (1UL << 15));  /* store/AMO page fault  */
   csrw("mideleg", 0UL);          /* the timer stays machine's */
   ```

3. **Do not delegate the timer.** `mtimecmp` is a machine CSR and there is no
   `stimecmp`, so a supervisor handler could not dismiss the interrupt it was
   given. Preemption is machine-mode work here; the supervisor handles what it
   can finish.
4. **`ECALL`'s cause tells you where it came from** — 8 from user, 9 from
   supervisor, 11 from machine — so one handler distinguishes a syscall from a
   supervisor call without reading `SPP`.

`misa` reports the set honestly — `MXL = 2` with `A`, `I`, `M`, `S`, `U` — so
probing it to discover the privilege set works
([architecture](architecture.md#the-csrs-that-exist)).

**Interrupt latency is bounded below by a divide.** A multi-cycle operation that
has started must finish before a trap can be taken, so 66 cycles is the floor on
the worst case, and an interrupt is additionally deferred past any load, store or
AMO.

## Running one

Verilator is the inner loop; xsim gates; only Vivado reports resources. The
harnesses own `main()`, the clock and `eval()` — there is no Verilog testbench.

```
python scripts/py/vlt.py rv64_core --cc sim/verilator/harness/rv64_core_main.cpp \
  --keep --run-args "--elf /mnt/c/.../build/rv64/dhry.elf --max-cycles 20000000"
```

| harness | drives |
|---|---|
| `rv64_core_main.cpp` | a sparse memory model with an ELF64 loader, a console at `0x1000_0000` and an exit register at `0x1000_0008` |
| `rv64_sys_pe_main.cpp` | the fabric port only — `CU_DATA`, `CU_INST`, `CU_SIGNAL` |
| `rv64_syscore_main.cpp` | the host window and the node port, with `--latency N` for the fabric's answer delay |
| `rv64_pe_pair_main.cpp`, `rv64_syscore_pair_main.cpp` | two units on one fabric, images interleaved |

`--keep` retains the built harness; rebuilding a C++ model per run is what makes
an interactive model pointless. Verilator runs under WSL, so `--elf` must be the
`/mnt/c/...` form of the path and not the Windows one.

More on the two-simulator split: [workflow/simulate](../../../workflow/simulate.md).

## What the programming model does not give you

- **No standard library, no `malloc`, no `printf`.** The console is one byte per
  store, and formatting is the program's.
- **No file system, no host call, no semihosting.** `ECALL` is a trap into your
  own handler, and nothing is listening behind it.
- **No dynamic loading and no relocation at run time.** The image is written
  once at a fixed base.
- **No thread support.** One hart; `LR`/`SC` exist for cross-*agent* structures
  in shared memory, not for threads on this core.
- **No memory protection except page tables.** There is no PMP, so machine mode
  reaches everything and isolation exists only where Sv39 is switched on and the
  tables say so.
- **No ASID.** Every `SFENCE.VMA` sweeps the whole TLB, so an address-space
  switch costs a full refill. Programs that switch often should expect it.
- **No `stimecmp`**, so a supervisor cannot own the timer.
- **No queue of dispatches.** The mailbox holds one outbound flit and a 16-deep
  completion queue; a scheduler that wants more keeps its own list in memory.
- **No debugger.** `EBREAK` traps or halts; nothing attaches.
