---
title: The node dispatcher firmware
summary: The bare-metal framework that runs on every node's RV64 processor — its tree, how an image is built, what the dispatcher does with a package, and how a project plugs its compute units in.
tags:
  - architecture
  - cpu
  - rv64
  - software
---

# The node dispatcher firmware

The node's RV64 processor runs a small bare-metal program that takes
[packages](../../../spec/package-format.md) from the host over the
[node queue](../../../spec/node-queue.md) and dispatches them to the mesh's
compute units itself. The host submits and polls; it never touches a unit.

## The tree

```
software/firmware/
  build.py                       images -> build/fw/<image>.elf (+ .map .lst .size)
  tools/                         host drivers: memprobe, os, xfprobe, elf_run, two_node
  kohakuaccel/                   the framework
    arch/rv64/                   crt0.S (mtvec before anything can fault), node.ld
    hal/                         the fatal-trap path and the exit store
    include/ka/                  every public header, by layer
      hal/                         node.h (control region, mailbox), mem.h, cpu.h
      lib/                         stdio.h, string.h
      boot/  queue/  package/  dispatch/  unit/
      os/                          task.h, heap.h, mem.h
    lib/                         stdio (stdout ring; console tap before it), string
    boot/                        the boot block, first in the scratchpad
    os/task/                     tasks and the run queue; switch_rv64.S
    os/heap/                     region heaps; the node's four regions
    queue/                       the queue service: the dispatcher's task
    package/                     the interpreter: bind, relocate, run steps
    dispatch/                    the dispatch engine over the mailbox
    unit/                        the unit-class registry
    apps/dispatcher/  apps/memprobe/  apps/osprobe/
  kohakutpu/                     the project
    units/                       'MG' and 'VC' unit classes
    apps/xfprobe/                the transform bank's geometry and fault word
  tests/                         host/heap_trace.c: heap.c natively, for
                                 software/driver/tests/test_node_heap.py
```

`python software/firmware/build.py [image]` compiles every source of an image in one
call to the WSL `riscv64-unknown-elf` toolchain with
`-march=rv64ima_zicsr_zifencei -mabi=lp64 -mcmodel=medany -Os -Werror` and
`-DEXIT_ADDR=0x20000` (the control region's exit register), links against
`node.ld`, and refuses an image that does not fit the 32 KB instruction window
or the 32 KB scratchpad. The ISA string is the node-processor line of
[programming.md](programming.md) plus `zifencei`, which `rv64_syscore`
implements.

`node.ld` mirrors `rv64_syscore`'s arrays (`IMEM_WORDS 8192`,
`SPAD_WORDS 4096`): IMEM at `0x0` holds `.text` only, because fetch reaches it
and loads never do; SPAD at `0x1_0000` holds everything read with a load.
`.bootargs` is first in SPAD at a fixed address because the host writes it
there after the image and before `HR_BOOT`; a magic other than `KABOOT1` means
nothing was written and the firmware stops. `.ka_units` is the unit plug-in
table, and the last 6 KB of SPAD is the boot stack.

crt0 zeroes every register (the core leaves reset at PC 0 with them
undefined), sets the stack, and installs the trap vector before anything can
fault, since `mtvec = 0` halts on an exception. The firmware runs in machine
mode with interrupts masked, so every trap is an exception and fatal: the
vector hands `mcause`, `mepc` and `mtval` to C on a fresh stack top, because
the interrupted stack may be what faulted. crt0 zeroes
`.bss` at about 7 cycles a word, so stacks and tables their owner initialises
are declared `KA_NOINIT` (`node.ld`'s `.noinit`, never zeroed): the
dispatcher's 10 KB of them would otherwise cost 9,200 cycles before `main`
(measured: `main` starts at cycle 2,360 with them in `.noinit`, 11,551
without). The ready banner reports the cycle counter at `main` and at ready.
Images:
`kohakutpu_node` (the dispatcher with KohakuTPU's units), `kohakuaccel_node`
(the dispatcher with the generic class only), `memprobe` (a measurement of
the processor's memory paths, driven by `software/firmware/tools/memprobe.py`)
and `osprobe` (tasks and heaps checked on the node, driven by
`software/firmware/tools/os.py`).

## What happens to a package

1. **Header.** Magic, version, every section inside the stated length, the
   counts within what the firmware holds (32 units, 64 buffers), the machine
   signature against the one computed from the boot block's unit table, and the
   checksum when asked for.
2. **Units.** Each table entry gets its class (below) and a credit: the
   package's, lowered by the class's, capped by the node-wide bound. A unit
   never holds more than its credit in flight (its instruction FIFO,
   control-registers.md §2.4). The node-wide bound is the boot block's
   `cq_depth` less the package's `ack_reserve`, and is optional: with
   `cq_depth` 0 there is none, because the mailbox holds a completion it has no
   room for at the hub instead of dropping it. That hold is on `sn_hub`'s one
   inbound link, so a full completion queue stops every memory request queued
   behind the completion: the engine therefore also drains whenever a send
   finds the mailbox completion queue half full (`KA_NM_CQ_DEPTH / 2`, from the
   `STAT` read it makes anyway), not only when it waits.
3. **Bindings.** One address per buffer, from the submission or the default.
4. **Steps**, in order. A `DISPATCH` reads each payload (four uncached loads),
   applies the relocations naming it, waits for credit, and writes the mailbox;
   completions are drained as item 2 says. `AWAIT` and `BARRIER` drain until
   their counts hold. A unit's completion means its DRAM writes
   have landed (`noc_cu_base` `ACK_FENCE`), so the barrier is the whole
   ordering. `MOVER` writes (register, value) pairs into the mover's register
   window (`0x00`–`0x7F`, at control region `0x100` + register; anything else
   fails `NO_REACH`) and waits for the moves they start. `RING` rings a
   mesh's doorbell once the mover is idle; `WAIT_BELL` waits on the
   hardware's 16-bit per-mesh doorbell counts against a base taken once at
   boot and advanced by each wait, so a ring that arrives before the package
   waiting for it starts is still found. `SIGNAL` posts a progress entry;
   `SETTLE` holds a cycle count.
5. **End.** An implicit barrier and one completion entry.

One package runs at a time per node: the interpreter's state is static, and
the mailbox, the mover and the doorbell counts belong to the task running it.

The engine writes a dispatch, not a flit: `DST` and the four payload words are
written at leisure and `GO` commits them. `GO` latches the flit whole, so `DST`
and the arguments may be rewritten while the previous flit is still offered,
but a `GO` while a flit is offered (`STAT[15]`) is dropped silently, so the
engine waits for that bit to clear first.

Every wait is bounded by the boot block's `timeout`; a package that fails lets
what is in flight retire, empties the mailbox, and reports the step and the
reason.

## Memory access

Addresses in the firmware are unit-global, as an instruction carries them:
mesh in `[37:36]`, the staging aperture flag in `[39]`. `ka_ld64`/`ka_st64`
go through the processor's **uncached alias**, address bit 38
(`rv64_syscore.v`, cleared on the way out), so they see what another agent
wrote with no cache maintenance; staging is uncached anyway. One uncached
access is one node-port round trip, about 30 processor cycles per 8 bytes on
the simulated card (`software/firmware/tools/memprobe.py`). Cached DRAM is shared only
with a D-cache flush or invalidate (control region `0x1C8`) around it.

The queue and its indices are always accessed uncached. An uncached store
completes before the next one issues, so a completion entry is in memory
before the tail that publishes it. A package in cacheable DRAM is read
through the L1 after one invalidate (the host wrote it behind the cache's
back), so a 32-byte payload is one line fill; anywhere else, staging or DRAM
outside the node's cached range, every read is uncached.

The queue service polls, by design: the host polls the completion tail and
the firmware polls the submission tail through `ka_wait`, so the run queue's
other tasks run while the ring is empty. The firmware resumes its indices
from the region instead of zeroing them, so a restarted firmware continues
the queue the host still holds; the host zeroes them at init. Every queue
field has one writer and its own 32-byte line, because the host writes in
32-byte granules and zero-fills what a write does not carry.

`memset`, `memcpy`, `memmove` and `memcmp` are byte-wise because misaligned
accesses fault (there is no fixup). `ka_printf` prints hex by shifts because a
divide costs 66 cycles.

## Output

All output -- `ka_printf`, `ka_puts`, `ka_putc` -- goes to the queue's
flow-controlled stdout ring once the queue service has set it
([node-queue.md](../../../spec/node-queue.md) §5). Before that, and in an image
with no queue, it goes to the console tap (`R_CONSOLE`), which is a DEBUG tap:
the load window keeps 256 unread bytes and silently overwrites beyond them
(`rv64_load_win.v:88-107`). `ka_debug` writes the tap directly, for short
lines only; the fatal-trap path writes one there as well as to the ring,
because a ring broken by the fault would swallow the only report. Input is the
stdin ring once set, else the control region's stdin register. `osprobe`
prints only failures: the host drains the console tap one byte per read and
pop, two host round trips per character.

## Tasks and the run queue

`ka/os/task.h`. A **task** is a routine with its own stack, run by a
cooperative round-robin run queue on the node's one hart:

```c
static uint64_t stack[512] __attribute__((aligned(16)));
static struct ka_task t;

ka_task_spawn(&t, "worker", fn, arg, stack, sizeof stack);
ka_sched_run();                          /* until every task has returned */

/* inside a task */
ka_yield();
ka_wait(ready_fn, ctx, timeout_cycles);  /* 0, or KA_ST_TIMEOUT */
ka_sleep(cycles);
```

- Nothing preempts a task: it runs until it yields, waits or returns. A wait
  names a **predicate the scheduler polls** — the node polls by design — so a
  task blocked on a unit, a doorbell or the host costs one predicate call per
  pass while the others run.
- A switch saves `ra`, `sp` and `s0`–`s11` (a 112-byte frame) and nothing
  else: every switch is a call, so the caller-saved registers are already dead.
- Each stack's lowest word is a canary checked on every switch back; an
  overrun stops the node with exit `0xE2…` | the task's run count. Stacks are
  filled at spawn, so `ka_task_stack_used` reports the deepest use.
- Outside any task `ka_yield` returns at once and `ka_wait` spins on its
  predicate, so code written against this runs with or without a scheduler.

The dispatcher runs the queue service as its first task (4 KB stack). The
service waits for the SQ through `ka_wait`, and every wait of a running
package — credit, `AWAIT`, barrier, mover, doorbell, a full CQ — yields. One
package runs at a time; its task owns the mailbox, the mover and the doorbell
counts. On `STOP` the service prints its stack use, switches and idle passes.

## Region heaps

`ka/os/heap.h` is a heap over one span of unit-global memory with its block
table in the caller's array; `ka/os/mem.h` gives the node four of them,
configured and used from the host through the queue
([node-queue.md](../../../spec/node-queue.md) §7) or from firmware:

```c
ka_mem_configure(KA_MEM_DRAM, base, bytes, 64);
uint64_t a;
if (ka_mem_alloc(KA_MEM_DRAM, 4096, 256, tag, &a) == 0) { ...; ka_mem_free(KA_MEM_DRAM, a); }
```

First fit by address, whole granules, merge on free with both free
neighbours, the table (64 entries per region, 24 B each, 6 KB for the four) in
the scratchpad. A heap never reads or writes the span it manages, so a heap
over uncached or host-shared memory costs no memory traffic and a stray host
write cannot corrupt it. Region 0 is by convention the node's DRAM and 1 its
staging; what each covers is the host's choice through the queue's `HEAP`
entry. One `osprobe` churn op with its table check costs about 3,800 cycles at
12 live blocks on the card model, so the node runs a short churn and the long
traces run natively. `kohakuaccel.driver.node.heap` is the same policy in
Python; `software/driver/tests/test_node_heap.py` runs `heap.c` natively
on random traces and requires the two to agree on every status, address and
table.

## Finding the units

Given no unit table in the boot block, the firmware enumerates its mesh before
it serves the queue: a `CU_CTRL` read of `CU_CAPS` through the mailbox (the
`DST` register's type field selects the flit type) to each coordinate in
`0..scan`, the reply read back whole from the mailbox's RX queue. The table is
published in the queue region, and the machine signature is computed over it.
The request carries the register index at payload `[247:240]` (`ARG3[55:48]`);
the value comes back at payload `[239:176]` (`P3[47:0]` above `P2[63:48]`). A
coordinate outside the router grid is clamped onto a real unit, which answers
with its own source, so a reply counts only from the coordinate that was
asked, and the RX queue is emptied first so an earlier exchange's late reply
is not read as this one's. `KA_ENUM_WAIT` (1,500) cycles without an answer
from the coordinate asked means nothing is there. Every non-signal flit that
arrives while a package runs is popped and counted, because a held one would
stall the hub.

## Plugging a unit in

A project describes each of its compute-unit types once:

```c
#include <ka/unit/unit.h>

KA_UNIT_CLASS(my_unit) = {
    .type = 0x4D47,                      /* CU_TYPE, 'MG' */
    .name = "MG",
    .credit = 0,                         /* no bound beyond the package's */
    .classify = ka_unit_generic_classify,
    .describe = my_fault_printer,
};
```

The linker gathers every class into one table; the dispatcher looks a
package's units up by type. `classify` turns a completion's signal code into
*retired*, *acknowledgement* or *fault*; `describe` prints what a fault's
argument means. A type with no class gets the framework's generic reading of
the centrally allocated signal codes: `INST_COMPLETE`, `BATCH_COMPLETE` and
`BARRIER` retire, `DATA_RECEIVED` is an acknowledgement, `FAULT` fails the
package.

KohakuTPU registers two classes, both with the generic reading. `MG`, the
matmul cluster: `FILL`, `GEMM` and `DRAIN` each retire with one completion,
and a node-addressed `DRAIN` is also acknowledged by its receiver with
`DATA_RECEIVED`. `VC`, the vector core: a `RUN` retires with the kernel's
cycle count as its argument, or faults with `vec_core.v`'s fault code in
`arg[7:0]`, which the class prints by name.
