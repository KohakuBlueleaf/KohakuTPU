# KohakuAccel OS

The operating system for the system nodes of any KohakuAccel machine: L0
machine layer (M), one kernel per node (S), user programs (U) with threads
and shared memory across nodes, and generic dispatch to compute units.

Documentation: [docs/os](../../docs/os/README.md) — architecture, memory,
boot, the system-call ABI, messaging, dispatch, boards, applications,
building and running, measured performance, hardware constraints.

## Quickstart

```
# images: build/os/{kos,m0,m0w}.elf (LLVM from $KOS_LLVM or PATH)
python software/os/tools/build.py --out ~/kos/out

# a 4-node core-only model, and the OS on it
python software/os/sim/build.py --nodes 4 --shared 1 --out ~/kos/build/core4s
cd software/os/tools
python run_core.py --board core4s --model ~/kos/build/core4s \
    --image ~/kos/out/kos.elf --cmd "hello; preduce 4096; bench"

# a Verilated card model, or the card (--jtag)
PYTHONPATH=../../driver:../../language:../../compiler \
python run_card.py --board card_v9t6_2n --model ~/ktpu/build/t2s/vlt_card_v9t6_2n \
    --image ~/kos/out/kos.elf --cmd "hello; units; mg; mover"

# host tests
KOS_BUILD=~/kos/build KOS_OUT=~/kos/out pytest software/os/tests/host -n 1
```

Details of every option: [docs/os/use/building.md](../../docs/os/use/building.md).
