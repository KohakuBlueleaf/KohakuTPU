---
title: KohakuAccel OS hardware constraints
summary: The behaviours of the RV64 system node and the interlink that the OS is built around — each with how it shows, where it was measured, the RTL behind it, and what the OS does about it.
tags:
  - os
  - hardware
  - constraints
---

# Hardware constraints and how the OS handles them

Each section: what the hardware does, the evidence (with where it was
measured), and the OS's handling. RTL references are to the current
`src/` tree.

## Sv39 back-to-back accesses

**Behaviour.** With translation on, a load or store that enters execute in
the cycle right after a multi-cycle (cached or uncached) load or store
completes, and whose translation hits the TLB, is issued with **the first
access's physical address**: a load returns the first one's data, a store
writes the first one's location. One instruction in between avoids it. A
TLB miss on the second access re-issues it correctly.

**Evidence.** `software/os/tests/xlat`: 18 cases of two or three accesses,
each run in S with `satp` on and in M, identity mapped; identical on the
repository's `rv64_node_pair` bench and the `core2` model. 4 of 18 differ:

| case | S, satp on | M |
|---|---|---|
| DRAM load; scratchpad load | `7003`, **`7003`** | `7003`, `5003` |
| DRAM; DRAM; DRAM loads | `7001`, **`7001`, `7002`** | `7001`, `7002`, `7003` |
| DRAM load; store `5a5a` to scratchpad | DRAM word **becomes `5a5a`**, scratchpad 0 | DRAM `7001`, scratchpad `5a5a` |
| store `77` to DRAM[5]; load DRAM[3] | **`77`** | `7003` |

with a `nop` or an ALU instruction between the two accesses every case
matches. The same source through the memory-gap pass: 0 of 18 differ
(`tests/host/test_xlat.py` asserts 4 and 0).

**RTL.** `rv64_mmu.v:200` registers `resolved_q <= resolved && same_req`;
in the last cycle of access A the port still carries A, so in the next cycle
— when B first appears — `busy` (`rv64_mmu.v:206`) is low for B and `pa`
(`rv64_mmu.v:207`) is A's registered TLB row and offset;
`rv64_syscore.v:526` starts B on that cycle and `:542-547` capture A's
address. The instruction-fetch side holds its translation two cycles
(`rv64_syscore.v:364-369`); the data side does not.

**Handling.**

- **Only user mode runs translated.** L0 and the kernel run with `satp` 0;
  the kernel's trap entry writes `satp` 0 before its first memory access and
  `k_resume` writes the user `satp` after its last.
- **User code is built through `tools/memgap.py`**, which puts a `nop` after
  every memory instruction followed directly by another (723 in the `kos`
  user side). `build.py --no-memgap` builds without it.

## A slot read needs one more read

**Behaviour.** The interlink's sig word reports the selected signal slot's
number and its count, but the count comes through one more register than
the slot field: for one read after a new selection, the word names the new
slot and carries **the previously selected slot's count**.

**Evidence.** `software/os/tests/sigslot`: node A rings node B's slot 5
three times; B alternately selects slot 0 (count 0) and slot 5 (count 3)
64 times. On `rv64_node_pair` and `core2` alike: the field matched on the
first read all 64 times; the count in that word was wrong 64 of 64 times;
the next word's count was wrong 0 of 64 (`tests/host/test_sigslot.py`).

**RTL.** `mag_ilink.v:993` writes the select `sig_sel`; `mag_ilink.v:280`
reads `sig_q <= sig_mem[sig_sel]`; `mag_ilink.v:295-296` builds the word
from `sig_sel` (the field) and `sig_q` (the count).

**Handling.** `hw_slot()` (`include/kos/hw.h`) selects, waits until the
field names the slot, then reads the word once more and takes that count:
19 cycles per slot read (m0 `lat.slot_select_read`, the same on silicon and
the models).

## Interlink state survives a processor reset

**Behaviour.** A node's processor reset (`HR_BOOT`) does not reset its
interlink: the signal-slot counts, the 16-bit count of rings the node has
sent, the fault bits and the staging stores keep the values of the previous
run. The node's mailbox, completion queue and dispatch engine *are* reset
with the processor.

**Evidence.** On silicon (v9t6, 4 dies), an image whose nodes count rings
from zero hangs on the second load after any run that rang: its first ring
waits for room while `rings issued − rings sent ≥ 4`, with *issued* 0 and
*sent* left over from the previous run (every node's log: banner, then no
progress). A Verilated model starts from power-on and does not show it;
`run_core.py --boots 2` (processors held in reset between boots, interlink,
staging and DRAM kept) reproduces it on `core2`: such an image passes boot 0
and every node hangs in boot 1.

**RTL.** `mag.v:789` resets the interlink (`u_il`) with the system
`resetn`; the mailbox and dispatch engine take `core_rstn`
(`rv64_syscore.v:806`, `:825`), which `HR_BOOT` drives
(`rv64_syscore.v:877-885`).

**Handling.** Every image starts through `include/kos/start.h`
([boot.md](../design/boot.md#the-start-handshake)): each node clears its own
slots (`IL_CTL` bit 3), takes the interlink's sent count as its ring count,
and node 0 starts the others only after each has posted this boot's run id
(fresh per boot) into node 0's staging. With it, the silicon runs follow one
another on the same card with no reset (silicon batch 2: five loads in a row,
all pass), and `test_second_boot` boots each image twice in one model.

## Atomics are not atomic across nodes

**Behaviour.** AMO and LR/SC instructions on the same DRAM word from
different nodes do not serialise: each node's operation is atomic only
against itself.

**Evidence.** `core4s` model (four nodes on one RAM), m0 `amo`: 4 nodes ×
2000 increments of one shared word, expected 8000 — `amoadd` gave 2001–3002
over runs, LR/SC 2000 with 0 SC failures on every node. On the card a load
does not cross the interlink at all, so no DRAM word is shared by two dies.

**Handling.** `SYS_ATOMIC` runs every operation at the word's **home node**
on its uncached alias, serialised by that node's kernel
([messaging.md](../reference/messaging.md#atomics-at-the-home-node)); locks
are built on it or on the bakery algorithm with plain uncached accesses
(store-buffering and message-passing litmus on uncached shared DRAM:
0 of 256 bad outcomes each, m0 `sc` on `core4s`).

## No coherence between L1s; whole-cache maintenance only

**Behaviour.** Each node's 64-line write-back L1 is private. The only
maintenance is a whole-cache sweep (`DCACHE`: flush, invalidate, or both).
`DCACHE` busy rises only once the sweep starts, a few cycles after the
write, so a read right after the write sees "not busy".

**Evidence.** m0 `lat` (cycles): flush of 64 dirty lines 1486 on silicon
(918 on `core2`/`core4s`), a clean flush 144, invalidate 76 (78),
flush + invalidate of 64 dirty lines 1552 (984). Reading busy immediately
after the write measured an 8-cycle "flush" of 64 dirty lines.

**Handling.** The kernel waits for busy to rise, then to fall
(`hw_dcache`). Shared memory is coherent at synchronisation points by
release (flush) and acquire (flush + invalidate) sweeps, or mapped uncached
(`shared_uncached`); see [memory.md](../design/memory.md#coherence-at-synchronisation-points).

## The page-table walker

**Behaviour.** The walker reads page tables **uncached** and **does not set
A or D**: a leaf with A clear faults on a load (cause 13), with D clear on a
store (cause 15), and the PTE stays unchanged. A changed PTE is not seen
until `sfence.vma`. A gigapage whose physical page number is not 1 GB
aligned faults. A PTE written cached into DRAM and not flushed is not seen.

**Evidence.** m0w `walk`, S translated, on `core2`, `core4s` and silicon
(batch 2 run 2), every value identical: A-clear load fault 1 (cause 13, PTE
`0x07`), D-clear store fault 1 (cause 15, PTE `0x47`), old frame still read
without `sfence.vma` 1, misaligned gigapage fault 1, DRAM PTE unflushed
fault 1 / after flush 0.

**Handling.** L0 sets A and D in every leaf (and G for non-user leaves),
keeps tables in the local staging store or writes them through the uncached
alias (`pt_dram`), uses a superpage only where virtual address, physical
address and size are all aligned, and runs `sfence.vma` after every change
([memory.md](../design/memory.md#the-soft-mmu)).

## M-only timer and cycle CSRs

**Behaviour.** `mtimecmp` and `mcycle` are machine-only on this core.

**Handling.** The kernel sets its preemption timer through the L0 call
`SET_TIMER`; time and cycle counts are read from the `time` and `cycle`
CSRs, which S and U may read. Timer lateness from the compare to the S handler: 65 average, 71
worst (m0 `trap`, silicon batch 2; 65–66 / 69–71 on the core models).

## The console drains slower than a node prints

**Behaviour.** On the card the host drains each node's console register
over JTAG, slower than a node prints; a node that prints faster than that
loses bytes from the register's FIFO.

**Handling.** Every console byte also goes into the node's **log area**,
written through the uncached alias, and the run scripts read it back after
the run (and after a timeout); `run_card.py` reports the longer of the two.
On silicon batch 2, node 0's log area held the m0 probe image's complete
856-byte output.

## Verilated 2-die card model: DRAM windows alias

**Behaviour.** In the 2-die card model, the DRAM behind mesh 0 and mesh 1
is one array: the same offset under either mesh is one cell.

**Handling.** The card boards carve the dies' spans at different offsets
(`layout: carved`), which is correct on silicon too
([boards.md](../reference/boards.md#the-boards)).
