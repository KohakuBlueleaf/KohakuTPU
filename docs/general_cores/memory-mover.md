# The memory mover

**Moved.** This grew past one document and now has its own folder:
[`../memory-mover/`](../memory-mover/).

| | |
|---|---|
| [`../memory-mover/README.md`](../memory-mover/README.md) | scope, the two target workloads, and the work order |
| [`../memory-mover/arch.md`](../memory-mover/arch.md) | placement, datapath, buffers, sizing, ordering |
| [`../memory-mover/isa.md`](../memory-mover/isa.md) | the command and descriptor encoding |
| [`../memory-mover/prng.md`](../memory-mover/prng.md) | the noise generator beside it |
| [`../memory-mover/compiler.md`](../memory-mover/compiler.md) | what the compiler emits, and view materialisation |

The short version, for readers arriving from [`cores.md`](cores.md) or
[`README.md`](README.md): the mover is a **new client of MAG**, an AXI master
beside the memory ports and the host upload path, with no NoC endpoint. It moves
bytes from a descriptor without computing on them -- layout conversion, gather,
and region fill -- and it is ranked ahead of the general cores because it is the
only one of the two that unloads the vector cores, which measurement says are
the bottleneck.

Together the two close both halves of `README.md` §1: a general core decides
*which*, the mover moves the bytes.
