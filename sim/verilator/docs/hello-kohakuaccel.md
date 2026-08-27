# A Verilator dev platform for the RV64 system node

The RV64 system node runs as a compiled Verilator model with a C++ harness that
plays the host: it loads a bare-metal program, feeds it input, captures its
output, and — for the I-cache demos — supplies and rewrites code in DRAM. This is
the loop a **KohakuAccel OS** developer works in: write a program, build it in a
second, run it with real I/O, no FPGA.

Three example programs exercise the abilities the node grew for the OS:

| program | ability | proves |
|---|---|---|
| `tests/rv64/hello_kohakuaccel.c` | **I/O** (stdin + stdout) | a program reads a line and answers it |
| `tests/rv64/icache_demo.c` | **I-cache** (code from DRAM) | code that lives in DRAM runs, and hot code stays resident |
| `tests/rv64/fence_demo.c` | **Zifencei** (`fence.i`) | rewritten code is picked up only after `fence.i` |

## How the abilities are wired

- **stdout** — the program stores a byte to `R_CONSOLE` (`CTRL_BASE + 0x08`); the
  harness watches `dbg_console`.
- **stdin** — a queue in `rv64_syscore.v`; the host pushes bytes through
  `HR_STDIN` on the slave window before boot, the program reads `{valid, byte}`
  at `R_STDIN` (`CTRL_BASE + 0x30`) and writes it to pop.
- **I-cache** — `rv64_icache.v`, a small read-only cache over the cached (DRAM)
  range; a fetch at or above `0x8000_0000` fills a line from DRAM. Code below the
  node base still comes from the on-chip window.
- **`fence.i`** — decoded by the core, surfaced as `fence_i_o`, and wired to the
  I-cache's invalidate.

## Prerequisites

Verilator (in WSL, `Ubuntu-24.04`) and `riscv64-unknown-elf-gcc`, as
[setup.md](setup.md) describes. Paths below are the WSL view of the Windows tree
(`/mnt/c/...`); replace `<co>` with your checkout root.

## 1 · I/O — hello_kohakuaccel

```bash
riscv64-unknown-elf-gcc -march=rv64ima_zicsr -mabi=lp64 -mcmodel=medany \
  -nostdlib -nostartfiles -ffreestanding -O2 \
  -DEXIT_ADDR=0x20000 -T tests/rv64/link_sys.ld \
  tests/rv64/crt0.S tests/rv64/hello_kohakuaccel.c \
  -o build/rv64/hello_kohakuaccel.elf -lgcc

python scripts/py/vlt.py rv64_syscore \
  --cc sim/verilator/harness/rv64_syscore_main.cpp --keep \
  --run-args '--elf /mnt/c/<co>/build/rv64/hello_kohakuaccel.elf \
              --stdin Kohaku --expect "Nice to meet you, Kohaku"'
```

Prints the banner, then `Nice to meet you, Kohaku!` — the name comes from stdin,
so any `--stdin <name>` changes the greeting. The model is kept under
`build/vlt_rv64_syscore/obj_dir/vsim`, so later runs need no rebuild.

## 2 · I-cache — icache_demo

`dram_func` is placed in `.dram_text`, which the link map puts at `0x8000_0000`;
the harness loads it into node memory, so every fetch of it is an I-cache access.

```bash
riscv64-unknown-elf-gcc -march=rv64ima_zicsr -mabi=lp64 -mcmodel=medany \
  -nostdlib -nostartfiles -ffreestanding -O2 \
  -DEXIT_ADDR=0x20000 -T tests/rv64/link_sys.ld \
  tests/rv64/crt0.S tests/rv64/icache_demo.c \
  -o build/rv64/icache_demo.elf -lgcc

./build/vlt_rv64_syscore/obj_dir/vsim \
  --elf /mnt/c/<co>/build/rv64/icache_demo.elf --expect "icache ok"
```

It calls the DRAM function 257 times and reports `node 1 reads` — one fill, then
every call hits the cache.

## 3 · Zifencei — fence_demo

`fence.i` needs the extension enabled in `-march`:

```bash
riscv64-unknown-elf-gcc -march=rv64ima_zicsr_zifencei -mabi=lp64 -mcmodel=medany \
  -nostdlib -nostartfiles -ffreestanding -O2 \
  -DEXIT_ADDR=0x20000 -T tests/rv64/link_sys.ld \
  tests/rv64/crt0.S tests/rv64/fence_demo.c \
  -o build/rv64/fence_demo.elf -lgcc

./build/vlt_rv64_syscore/obj_dir/vsim \
  --elf /mnt/c/<co>/build/rv64/fence_demo.elf --expect "fencei ok"
```

Expected:

```
call 1 (funcA)            : 5898270
[host] copied 32 bytes of DRAM code 80000010 -> 80000000
call 2, no fence (stale)  : 5898270      <- cache still holds old code
call 3, after fence.i     : 720926       <- fence.i dropped it, refilled new code
fencei ok
```

## What this shows

The whole KohakuAccel OS inner loop closes under Verilator: write a program, run
it in seconds, give it input and read its output, run code from DRAM of any size,
and reload code on the card and have `fence.i` make it visible — with the harness
standing in for the host. This is the substrate the OS and its user programs are
developed on before any silicon is involved.

> Verilator validates **behaviour**. Whether the I-cache in the fetch path closes
> 300 MHz is an out-of-context synthesis question, not a Verilator one.
