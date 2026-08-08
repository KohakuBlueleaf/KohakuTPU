# The control-program ISA

What the host writes into `main_orch` and what the machine does with it.

Source: [`src/kohakuaxi/main_orch.v`](../../src/kohakuaxi/main_orch.v),
[`src/ktpu/hw/device.py`](../../src/ktpu/hw/device.py).

---

## 1. What it is for

The host loads a list of commands, writes `GO`, and reads one register until a
flag sets. Between those two host transactions the orchestrator drives the whole
computation over its own AXI master — including every poll.

That is the entire reason this ISA exists. Polling `NODE_STATUS` from the host
makes the machine's speed a function of the link: JTAG is milliseconds per
transaction, PCIe is microseconds. Moving the poll onto the card makes the same
program run at the same speed on both, and the link only affects how long
loading takes.

## 2. Three opcodes

A command is four 64-bit words: `{op, addr, data, mask}`.

| op | value | meaning |
|---|---|---|
| `WR` | 1 | one AXI write of `data` to `addr` |
| `POLL` | 2 | read `addr` until `(rdata & mask) == data` |
| `DONE` | 3 | stop, latch `data` as the completion code, raise `done` |

Anything else stops the program and sets `err`. Slot storage resets to `op = 0`,
so a program that runs off its own end lands on an unwritten slot and halts with
`err` rather than executing whatever was there from the previous round.

There are no branches, no arithmetic and no registers, and that is deliberate.
The machine's entire control surface is memory-mapped, so "stage a program,
dispatch it, wait for it" is already writes and a poll. Adding expressiveness
here would duplicate the host, which is a general-purpose computer sitting on
the other end of the link.

The cost is that loop structure has to be unrolled by the driver. A tiled GEMM
therefore emits one kick per pass and is cut into rounds when the unrolled form
stops fitting — see [kernel.md](kernel.md) §4. Adding a loop opcode is on the
plan; it is not here.

### 2.1 What `POLL` does not do

`POLL` has no timeout and no iteration limit. A condition that never becomes
true is an infinite loop with `busy` stuck high. `POLLS` counts retries and is
the only instrument for it: a stuck program shows a `PC` that does not move and
a `POLLS` that climbs.

`WR` is a single 64-bit beat — `awlen = 0`, `wstrb = 0xFF`, `wlast = 1`. There is
no burst form, so uploading bulk data through the command RAM would cost one
command per word. That is why the driver uploads instruction flits directly
instead (§5).

## 3. Register map

The slave decodes the low 16 bits of the address. Bit 28 of the full address
selects MAG over the orchestrator (`axi_xbar2.v`, `SEL_BIT = 28`), so
`ORC_BASE = 0x0000_0000` and `MAG_BASE = 0x1000_0000`.

| offset | name | access | contents |
|---|---|---|---|
| `0x0000` | `CTRL` | W | `[0]` GO |
| | | R | `[0]` busy, `[1]` done, `[2]` err |
| `0x0008` | `PC` | R | current command index |
| `0x0010` | `CODE` | R | the `DONE` code |
| `0x0018` | `POLLS` | R | polls executed since `GO` |
| `0x1000` | `CMD` | W | command `n`, field `f`, at `0x1000 + n*32 + f*8` |

Field order is `f = 0` op, `1` addr, `2` data, `3` mask.

The 32-byte stride is not packing waste — it is what makes the decode free. The
field selector is `waddr[4:3]` and the command index starts at bit 5, so both
are plain slices with no multiply. A 28-byte stride would need a divider in the
write path for one word of RAM per command.

> `CTRL` bit 1 is commented `ABORT` in the RTL's register map. **It is not
> implemented** — the write path decodes only `s_wdata[0]`. There is no way to
> stop a running program short of reset.

`irq` is tied directly to `done`. It is a level, not a pulse, and nothing in the
bench consumes it.

## 4. Execution

```
   IDLE ──GO──► FETCH ──┬── WR   ─► AW/W ─► wait B ─► pc+1 ─┐
                        │                                   │
                        ├── POLL ─► AR ─► wait R ──┬─match──►┤
                        │                          └─retry──┘
                        └── DONE ─► latch code, done=1 ─► IDLE
```

`GO` resets `pc`, `err` and `POLLS`, and sets `busy`. `DONE` clears `busy` and
sets `done`. An unknown opcode does the same but also sets `err`, so a corrupt
command RAM reports `pc` at the bad command instead of running a corrupt
program — which is how a field-offset bug in the first version of this bench was
found in one read.

## 5. Capacity

| limit | value | set by |
|---|---|---|
| commands | **128** | `NCMD` parameter |
| command RAM window | `0x1000`–`0x1FFF` | `waddr[15:12] == 1` |
| PC width | 7 bits | `$clog2(NCMD)` |

`NCMD * 32` must not exceed `0x1000`, which caps `NCMD` at 128. At exactly 128
the window is exactly full.

**What happens when it is exceeded.** Nothing reports an error. Command 128
would be written at `0x2000`, which fails the `waddr[15:12] == 1` test, falls
into the register decode, matches nothing, and is dropped. The program then runs
to command 127 and the 7-bit `pc` wraps to 0, so it restarts instead of halting.
The driver checks instead of relying on the hardware:
`kernel.plan` cuts rounds against `ncmd`, and `bench.write_files` raises if a
round still exceeds `NCMD`.

## 6. Setup data is not commands

`Program` holds two lists. `cmds` is the control program. `setup` is
`(addr, data)` pairs the host writes itself, before `GO`.

Instruction flits go in `setup`. Routing them through the command RAM would make
the host ship every flit twice — once into the command RAM and again when a `WR`
replays it into the staging RAM — and would make the program grow with the
problem size rather than with its control flow. The staging RAM is already AXI
addressable, so the copy buys nothing.

On the two-cluster GEMM in [`../mas/driver.md`](../mas/driver.md) that is 55
commands down to 15, and the gap widens with every cluster.

## 7. The shape of a program

```
   WR   SIG_DONE, 0                     clear the global completion count
   WR   PROG_CRED, INST_DEPTH           seed the ROUND's credit, once
   for each pass:
       POLL PROG_STAT, want run == 0
       WR   PROG_DST / PROG_BASE / PROG_LEN   (only what changed)
       WR   PROG_KICK, 1
   POLL SIG_DONE, want total flits      ONE poll for the whole machine
   DONE 0xC0DE
```

`PROG_CRED` sits outside the loop deliberately, and it is the one register here
that is neither shadowed nor repeated: re-seeding it per kick would destroy the
bound it exists to be. See [agent.md](agent.md) §6.1.

Every kick happens before any wait. Passes in a round have no dependency on each
other, so waiting between them would serialise clusters that share no data — the
measured cost is 1.97x on `C[32,32]` across two clusters.

See [agent.md](agent.md) for what those registers do, and [kernel.md](kernel.md)
for how a GEMM becomes this.
