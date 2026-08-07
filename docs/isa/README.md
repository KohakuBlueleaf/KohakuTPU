# Instruction sets

There are five of them, and they exist because each one is the cheapest way to
express a different question. The host writes a **control program** into the main
orchestrator, which is a list of writes and polls with no branches — the whole
point being that the host is not in the loop once it writes `GO`. Those writes
land in the **agent's dispatch registers** inside MAG, which name a staged
program and a destination node and kick it. The agent streams **CU instruction
flits** into the mesh, three opcodes that a cluster executes one at a time. Each
`GEMM` flit expands into hundreds of **ACU ops** against the resident output
tile, paced by the manager's sweep. Underneath all of it, operand and result
movement is the **memory protocol** between a CU and MAG.

Each level is narrower than the one above it and none of them can express the
level below. That is deliberate: the control program cannot loop, so the driver
unrolls; the CU cannot address DRAM affinely, so the driver computes addresses;
the ACU has no addressing at all, so the manager sweeps.

| level | what it expresses | document |
|---|---|---|
| host → orchestrator | writes and polls, one run per `GO` | [orchestrator.md](orchestrator.md) |
| orchestrator → agent | stage, dispatch, wait | [agent.md](agent.md) |
| agent → CU | fill, sweep, drain | [cluster.md](cluster.md) |
| manager → ACU | load, add, emit | [acu.md](acu.md) |
| CU ↔ MAG | read, write, quantise | [memory.md](memory.md) |

Above all five, [kernel.md](kernel.md) is the driver-side contract: how a GEMM of
arbitrary size becomes passes and rounds, and why the operand layout is what it
is.

---

## One pass, end to end

`C[32,32] = A[32,128] @ B.T[32,128]` on the cluster whose manager sits at
`(x=1, y=1)`. Deliberately a small problem: `choose_tile` returns
`gm=8, gn=8, nk=4`, so the whole thing is a single pass with a single K chunk
and every count below is small enough to follow by hand.

The tile is small because the *problem* is, not because the machine is — the
padding discount picks the shape that wastes least on a 32×32 output, and
`mag_driver_tb`'s actual capacities are `TILES = 512`, `GA = 128`, `GB = 256`,
where a large GEMM gets `gm=16, gn=32, nk=4` and a 64×128×128 pass
([kernel.md](kernel.md) §1, [cluster.md](cluster.md) §4.7).

### 1. The host stages four flits

Four `CU_INST` flits, 5 x 64-bit words each, written straight into the staging
RAM at `0x1000_2000 + (slot*5 + w)*8`. **20 writes.**

| slot | op | fields |
|---|---|---|
| 0 | `FILL` | `addr = 0x0000`, `n = 32`, `sel = 0` (A) |
| 1 | `FILL` | `addr = 0x2000`, `n = 32`, `sel = 1` (B) |
| 2 | `GEMM` | `gm = 8`, `gn = 8`, `nk = 4`, `anchor = 40`, `acc = 0` |
| 3 | `DRAIN` | `addr = 0x4000`, `n = 64`, `last = 1` |

`n = 32` on each `FILL` is `gm*nk` and `gn*nk` — 32 L1 entries, well inside
`GA = 128` and `GB = 256`. `n = 64` on the `DRAIN` is `gm*gn`, well inside
`TILES = 512`. This example does not fill the machine; it is sized to be
followed by hand.

These are **setup**, not commands. They go directly to the card's staging RAM,
because routing them through the command RAM would ship every flit twice.

### 2. The host loads nine commands

Written at `0x0000_1000 + n*32 + f*8`, four words each. **36 writes.**

```
   0  WR    SIG_DONE,  0                     clear the global completion count
   1  WR    PROG_CRED, 32                    seed the ROUND's credit, once
   2  POLL  PROG_STAT, want 0, mask 0x1      dispatcher idle
   3  WR    PROG_DST,  0x11                  {y=1, x=1}
   4  WR    PROG_BASE, 0
   5  WR    PROG_LEN,  4
   6  WR    PROG_KICK, 1                     the write IS the launch
   7  POLL  SIG_DONE,  want 4, mask 0xFFFF
   8  DONE  0xC0DE
```

Commands 2–6 are the per-pass group and repeat for every pass in a round;
0 and 1 happen once before all of them, and 7–8 once after
([orchestrator.md](orchestrator.md) §7).

Then one write to `CTRL` with bit 0 set. **57 host writes in total**, after which
the host only reads `CTRL` until bit 1 sets.

### 3. The agent dispatches

`PROG_KICK` starts the dispatcher, which reads 20 words out of the staging RAM,
assembles four flits, rewrites each header — destination from `PROG_DST`, source
stamped with the agent's own coordinates — and pushes them into the mesh. Each
push costs one credit; `SIG_INST_COMPLETE` returns it.

### 4. The CU executes

**`FILL` A.** **One** `MEM_RD_REQ` flit — a streaming descriptor naming
`base = 0x0000` and `count = 32`, with `QUANT` set and `BLAYOUT = 0`. The memory
port walks the address sequence itself: per entry, one 8-beat 32-byte AXI burst
(256 bytes of FP16) through `mx_quant`, returning **4** `MEM_RD_RESP` flits of
32 int7 elements plus 4 E5M3 scales, each tagged with its entry index and word.
The CU reassembles each set of four into one 928-bit L1 entry and needs no
cursor of its own. 8,192 bytes of A read; 32 entries written, 128 responses.

**`FILL` B.** The same, with `BLAYOUT = 1`, which changes only the slot order
within the operand word.

**`GEMM`.** The manager sweeps `for kb in 0..3: for g in 0..7: for h in 0..7`,
reading `L1A[g*4 + kb]` and `L1B[h*4 + kb]` and issuing 256 commands to the
accumulator at tile address `g*8 + h`. Because `acc = 0`, the 64 commands with
`kb = 0` are `LOAD` and the remaining 192 are `ADD`. `gm*gn = 64` is well above
`REUSE_MIN = 5`, so no idle cycles are inserted.

**`DRAIN`.** 64 `EMIT` commands, one per resident sub-tile. Each result is one
256-bit word of 16 FP16 values, and they are **collected into bursts of
`WBURST = 8`** on the accumulator's port: one `MEM_WR_REQ` descriptor with
`len = 7` at `0x4000 + t*32`, then 8 `MEM_WR_DATA` flits. 64 sub-tiles is 8 such
bursts — **72 flits, not 128** — and the memory port reassembles each burst in
one write slot and issues it as a single 8-beat AXI transaction. 2,048 bytes of
C written, once.

### 5. Completion

Each retiring instruction sends a `CU_SIGNAL`. The first three carry
`SIG_INST_COMPLETE`; the `DRAIN` carries `last`, so it retires as
`SIG_BATCH_COMPLETE`. `SIG_DONE` counts all four regardless of code, command 7
matches, command 8 halts with `0xC0DE`, and `done` sets.

### Traffic summary

| | count |
|---|---|
| host `write64` | 57 |
| `CU_INST` flits | 4 |
| `MEM_RD_REQ` | 2 — one streaming descriptor per `FILL` |
| `MEM_RD_RESP` | 256 |
| `MEM_WR_REQ` + `MEM_WR_DATA` | 72 — 8 bursts of `1 + WBURST` |
| `CU_SIGNAL` | 4 |
| ACU commands | 320 (64 `LOAD`, 192 `ADD`, 64 `EMIT`) |

One `GEMM` flit became 256 accumulator commands, one `FILL` flit became 128
response flits, and four flits became a whole matmul. That ratio is the reason
for the layering: every level exists to stop the level above it from having to
say the same thing 256 times.

---

## Related

* [`../mas/driver.md`](../mas/driver.md) — the driver interface as first built,
  with measured concurrency numbers.
* [`../compute/tensor-isa.md`](../compute/tensor-isa.md) — the tensor-descriptor
  ISA agreed as the design. Not implemented; [cluster.md](cluster.md) is what
  exists.
* [`../noc/spec.md`](../noc/spec.md) — flit format and the NoC-level contract.
* [`../compute/accumulator.md`](../compute/accumulator.md) — why the accumulator
  is shaped the way [acu.md](acu.md) describes.
