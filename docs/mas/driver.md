# The driver interface — superseded

This document described the driver interface as it was **first built**: three
opcodes, one register map, and a hand-written two-cluster program. All of that
is still true and all of it now lives in [`../isa/`](../isa/README.md), described
against the RTL rather than against the one bench that existed at the time.

| what you wanted here | where it is now |
|---|---|
| `WR` / `POLL` / `DONE`, `main_orch`'s register map, what a bad opcode does | [`../isa/orchestrator.md`](../isa/orchestrator.md) |
| `PROG_DST` / `BASE` / `LEN` / `CRED` / `KICK`, staging, `NODE_STATUS`, `SIG_DONE` | [`../isa/agent.md`](../isa/agent.md) |
| what a program looks like, and why setup is not commands | [`../isa/orchestrator.md`](../isa/orchestrator.md) §6–7 |
| how a GEMM of arbitrary size becomes that program | [`../isa/kernel.md`](../isa/kernel.md) |

## Where the host driver lives now

`ktpu.hw.device` (transport + control program), `ktpu.hw.board` (the machine as
data), `ktpu.hw.fpga` (a live session), `ktpu.hw.runtime` (the device surface:
allocate, copy, compile, call). `scripts/py/run_fpga.py` is the entry point.

### The packing gap, and why `mm_mover` is the answer

**Recorded, not built.** Nothing below exists, and the first link is the only
one reachable today.

`runtime.copyin` is a raw byte copy, because that is what a tensor library's
allocator contract is — no shape, no dtype, no strides, and they cannot be
added without breaking it. But MAG fetches a **tile-major** FP16 image, which
`bench.upload` builds host-side. So today **the caller packs**, and that is
structural rather than a stopgap: the layout has to be fixed by something that
knows shape and stride, and `copyin` by definition does not.

**`mm_mover` is exactly that something, and it is currently unused.** It is two
`mx_tdesc` descriptor walkers with per-dimension counts, strides, axes and
steps — a strided gather/scatter engine, and row-major to tile-major is the
transformation such an engine exists to perform. The intended shape is:
`copyin` does a linear DMA into a staging region, then the mover repacks
on-device into the image MAG reads.

That reframes the mover: it is not a fill utility, it is the missing half of
the data path. The dependency chain is

    clean copyin -> on-device repack -> mm_mover -> a bitstream carrying the
    AXI command path (`A_MV_CFG`) -> the block design that wires it

On the shipped card `mv_cfg_en` has no net at all, so the mover cannot be
commanded and the PRNG inside it is equally unreachable. Host-side packing is
correct and shippable until that changes.

One driver-side constraint to honour when it does: a descriptor dimension is an
ORDERED PAIR of writes — `0x18` loads, `0x20` commits — and nothing may
interleave. A `write_block` that coalesced a descriptor into one contiguous
burst would be legal AXI and silently wrong, so that path must stay
word-at-a-time by construction rather than by comment.

---

Two things have changed since, and the old text is wrong about both.

**A program no longer contains the flits.** Instruction flits are *setup data*
the host writes straight into the staging RAM, and only control goes into the
command RAM. The two-cluster program below cost **55 commands**; the same work
now costs **15**, and the gap widens with every cluster. See
[`../isa/orchestrator.md`](../isa/orchestrator.md) §6.

**Dispatch credit is seeded once per round, not once per kick.** Credit keeps
instructions in flight below one CU's instruction FIFO; exceeding that depth
raises `noc_in_busy`, which backpressures the same mesh link the CU's memory
read responses arrive on, so it can never drain the FIFO that is blocking it.
A per-kick seed lets `P` kicks admit `P × n` instructions against a FIFO of 32.
`Program.seed_credits` in `src/ktpu/hw/device.py` carries the argument.

---

## What is still only here: the concurrency measurement

`PROG_BASE` is what lets two clusters run at once. Without it every kick
restarts at staging slot 0, so a second cluster's flits cannot be staged until
the first has consumed its own — which forces a wait between dispatches and
serialises clusters that have no data dependency at all.

Measured on `C[32,32]` across two clusters:

```
   serial       7928 ns    overlap    0 cycles
   concurrent   4032 ns    overlap  860 cycles     1.97x
```

1.97x of a theoretical 2x, which is the number that justifies
kick-everything-then-wait-once as the shape of every control program since.

> Concurrency also has to be true of everything downstream. Two clusters writing
> results at once interleave their flits in the mesh, and that took three
> separate fixes in `src/kohakumas/mag.v` before the concurrent answer matched
> the serial one.

## The first end-to-end run

`tests/mas/mag_system_tb.v`, via `tests/run_mag_sim.ps1`, both DSP models — as
it stood on the day it first passed:

```
   C[16,16] = A[16,32] x B[32,16], split by output column across 2 clusters

   orchestrator stopped at pc=50, DONE code = c0de
   cu0 f=2 g=1 d=1     cu1 f=2 g=1 d=1
   MAG memory: 12 reads, 16 writes  (via its AXI master, to the AXI RAM)
   257 checks, 0 errors
   worst 4.78e-4 (0.49 FP16 ULP)   mean 1.44e-4
```

> **Historical, and the counters no longer match.** The bench still runs and
> still passes, but it has grown: the shape is now `C[16,16] = A[16,64] ×
> B[64,16]` — K two blocks, so that a *fused* drain has a last K block that is
> not also its first — and the two clusters now take deliberately different
> routes to the same answer: cluster 0 quantises on every read and drains
> explicitly, cluster 1 runs from int7 uploaded through the quantising window
> and uses a fused drain. Re-run it for current figures rather than trusting the
> block above.

It carries a hand-built program, so it is a fixed-shape regression test;
`tests/mas/mag_driver_tb.v` is the one that exercises the tiling and arbitrary
shapes ([`../system.md`](../system.md) §6).
