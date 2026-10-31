---
title: Building and running KohakuAccel OS
summary: The toolchain and build.py, the memory-gap pass, the core-only Verilator models, run_core.py and run_card.py (models and the card), the log tools, and the host test suite.
tags:
  - os
  - build
  - simulation
---

# Building and running

Everything lives under `software/os/`:

```
software/os/
  machine/   L0 (M)                     include/kos/   the ABI headers
  kernel/    L1 (S)                     lib/           string, formatting (both sides)
  user/      libkos (U)                 boards/        board descriptions
  utils/     init, shell, ps, kstat     tools/         build, boot tables, run, logs
  apps/      probe/ cpu/ ktpu/          sim/           core-only models
  tests/     m0/ xlat/ sigslot/ host/
```

## Toolchain and images

LLVM (clang, ld.lld, llvm-objcopy, llvm-objdump, llvm-size) for
`riscv64`, `-march=rv64ima_zicsr_zifencei -mabi=lp64 -O2`. `build.py` takes
the tools from `$KOS_LLVM`, else `~/micromamba/envs/kos/bin`, else `PATH`.

```
python software/os/tools/build.py [IMAGE ...] [--out DIR] [-D NAME=VALUE] [--plain] [--no-memgap] [--list]
```

Every image is built with the hot-path definitions (`HOT`,
[hotpaths](../design/hotpaths.md)); `--plain` leaves them out.

| image | what | runs translated |
|---|---|---|
| `kos` | the OS: L0 + kernel (kernel side), libkos + utilities + apps (user side) | the user side |
| `m0` | the bare-metal probe image (no OS): latencies, rings, litmus, traps, instruction fetch | nothing |
| `m0w` | the same source, for its `walk` probe | its supervisor code |

Output, per image, in `--out` (default `build/os`): `<image>.elf` with
`.map`, `.lst` (disassembly with source) and `.size`. The kernel side and
the user side are compiled separately; each carries its own copy of `lib/`;
the user side is linked into one object whose only global symbols are its two
entry points. The `kos` image's sizes: kernel text 29 952 B (of the 32 KB
instruction window), L0 data 3056 B, kernel data 19 520 B, user text
90 112 B, user data 4096 B.

`-D` defines reach every source, both sides. The ones the tree reads:

| define | what |
|---|---|
| `KOS_PROF` | the kernel stamps the cycle counter along its trap path and sums the stages of one selected system call (`KOS_I_PROF`, the `kprof` app); kernel text +448 B |
| `KDS_MUT=n` | a mutant of the dispatcher or L0 for `test_kds.py` (n = 1–8, [dispatcher](../reference/dispatcher.md)) |

### The memory-gap pass

Code that runs translated is compiled to assembly first and passed through
`tools/memgap.py`, which puts a `nop` after every load, store, LR/SC or AMO
whose next instruction is another one (labels do not break the straight
line; a jump target has the jump in front of it). That is the software side
of [the Sv39 back-to-back access behaviour](../hardware/constraints.md#sv39-back-to-back-accesses).
It adds 723 nops to the `kos` user side and 494 to `m0w`. `--no-memgap`
builds without it, for a core that does not need it.

## Core-only models

A Verilator model of 1–4 system nodes on one interlink chain, no compute
units — the repository's two-node bench with its top replaced:

```
python software/os/sim/build.py --nodes N --shared 0|1 --out DIR [--dram-words W] [-j J]
```

`--shared 0` gives each node its own RAM (`core2`); `--shared 1` puts every
node on one RAM (`core4s`, `core3s`). The model is `DIR/obj_dir/vsim`
(harness `sim/harness/kos_cluster_main.cpp`); it runs about 60 k (2 nodes)
to 20 k (4 nodes) node cycles per second.

## run_core.py — an image on a core-only model

```
cd software/os/tools
python run_core.py --board core4s --model ~/kos/build/core4s --image ~/kos/out/kos.elf \
    --cmd "hello; preduce 4096; bench" [--flags poll,pt_dram] [--tick 200000] \
    [--max-cycles N] [--json out.json] [--log live.txt] [--quiet] \
    [--boots K] [--hold N]
```

It writes every node's boot table and the image's user part at each node's
private base, runs the model, and collects:

- `[i] ...` — node *i*'s console; `@ name value` lines become `measures`;
  any line with `FAIL` is listed in `fails`;
- every node's log area read back from the model (`nodes`: log text,
  bytes, `where`, its two words, thread switches);
- the model's exit status: 0 when every node exited 0.

`--boots K` runs the image K times in one model, a fresh boot table (run id)
each time, with the interlink, staging and DRAM kept between boots — what a
card does from one run to the next. `--hold N` leaves node N in reset (its
peers then wait in the start handshake, which the log areas show).

## run_card.py — the card, or a Verilated card model

```
PYTHONPATH=software/driver:software/language:software/compiler \
python software/os/tools/run_card.py --board card_v9t6_2n \
    --model ~/ktpu/build/t2s/vlt_card_v9t6_2n --image ~/kos/out/kos.elf \
    --cmd "hello; units; mg" [--flags engine] [--json out.json]
python software/os/tools/run_card.py --board card_v9t6_4n --jtag --image ... # silicon
```

| option | |
|---|---|
| `--model DIR` / `--jtag` | a Verilated card build, or the card through Vivado (JTAG) |
| `--load burn\|axi` | poke the model's arrays (default with `--model`) or stream through the load window as on silicon (default with `--jtag`) |
| `--paint yes\|no` | zero each node's span and the shared region first; default yes on the card (a never-written line is an ECC error; a cached store fills its line first), no on a fresh model (its DRAM reads zero) |
| `--timeout S` | wall-clock bound (default 1800) |
| `--out`, `--json` | output directory, JSON result |

Per node: hold the core, (paint), write the user image, the instruction
window and the scratchpad with the boot table, then start the nodes last to
first. While the nodes run it drains the console and **prints every node's
progress word every 30 s**; afterwards, also after a timeout, it reads each
node's log area and prints the longer of the log and the drained console,
then `status exit halt_pc where …` per node and
`run_card: PASS|FAIL; load …s, run …s`. A node's DRAM address is the
processor's with the mesh bits taken as the mesh.

`tools/klog.py` parses a log area (`parse`, `describe`); both run scripts use
it.

### Card models

| board | model | speed |
|---|---|---|
| `card_v9t6_1n` | `~/ktpu/build/v9t6/vlt_card_v9t6_1n` (1 die) | load ~55 s, ~35 k node cycles/s |
| `card_v9t6_2n` | `~/ktpu/build/t2s/vlt_card_v9t6_2n` (2 dies) | load ~110 s, ~8 k node cycles/s |

The 4-die boards run only on the card.

## The bare-metal probes

`m0` and `m0w` run under the same scripts with `--cmd` naming the probe
groups: `lat ring sc amo trap itext` for `m0`, `walk` for `m0w`:

```
python run_core.py --board core4s --model ~/kos/build/core4s --image ~/kos/out/m0.elf --cmd "lat ring sc amo trap itext"
```

`tests/xlat` (the Sv39 back-to-back cases) and `tests/sigslot` (the slot
read) are built by their tests and also run on the repository's
`rv64_node_pair` bench.

## Host tests

```
cd software/os/tests/host && KOS_BUILD=~/kos/build KOS_OUT=~/kos/out pytest --rootdir . -n 1
```

| file | needs | cases |
|---|---|---|
| `test_core.py` | core models `core2`, `core4s`, `core3s` in `$KOS_BUILD`; images in `$KOS_OUT` | every app on core4s, each paired with a knob; the message-only apps on core2; the non-TPU board; channel flow control; landing zones with `pt_dram`; two boots in one model; log areas; the progress word of a held node; `bench` |
| `test_card.py` | card models in `$KOS_CARD_MODELS` (default `~/ktpu/build`); `KOS_DRIVER_PATH` for `run_card.py` | 1 die: `units`, `mg` (mailbox and engine), `mover`; 2 dies (only with `KOS_SLOW=1`, about 2 minutes each): remote units, remote `mg`, mover into the other die's landing zone |
| `test_kds.py` | the core models; the card models and driver path for the unit cases; the LLVM toolchain (it builds the mutant images with `-D KDS_MUT=<n>`) | the dispatcher apps on `core2` and `core4s` (chain and reduction with and without `-u`, refusals, the arenas with first and best fit, one home per node, `kdsbench`); each mutant fails the app that witnesses its property (predecessors ignored, DEPs dropped, free extents never merged, predecessor order unchecked, L0 `QUERY` without the in-leaf page; on the 1-die card model: patches with the user address, no release, no acquire); `kdsmg` on 1 die (plain, `-e`, `-u`) and, with `KOS_SLOW=1`, on 2 dies |
| `test_prof.py` | `core2`; the LLVM toolchain (it builds a `-D KOS_PROF` image) | `kprof 32`: every selection profiles exactly its calls, the null call's kernel stages fit inside the round trip the caller timed; the plain kernel refuses `KOS_I_PROF` |
| `test_xlat.py` | `core2` | the Sv39 cases differ (4) without the memory-gap pass and do not (0) with it; the pass's output shape |
| `test_sigslot.py` | `core2` | the first sig word naming a slot carries the previous slot's count; the next one is right |

Each case is one short command on a fresh model and finishes in under a
minute; a full run of the default set takes about 15 minutes (`-n 1`; the
models are single-threaded and share the machine).
