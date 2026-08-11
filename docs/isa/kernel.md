# The driver-side contract

How a GEMM of arbitrary size becomes instructions the machine can hold.

Source: [`src/ktpu/hw/kernel.py`](../../src/ktpu/hw/kernel.py),
[`bench.py`](../../src/ktpu/hw/bench.py),
[`device.py`](../../src/ktpu/hw/device.py),
[`tensor.py`](../../src/ktpu/hw/tensor.py).

This is the layer above the ISA. Nothing here is hardware; all of it is what the
hardware's limits force software to do.

---

## 1. What the hardware holds

The machine holds one tile of the problem at a time:

```
   gm * gn     <= TILES         output sub-tiles resident in the accumulator
   gm * nk     <= bank_a        A's L1 entries, in ONE bank
   gn * nk     <= bank_b        B's likewise
   bank_x      =  min(l1_x // banks, L1_OFF_SPAN)      L1_OFF_SPAN = 256
```

with `TILES = 512`, `GA = 128` and `GB = 256` in `mag_driver_tb`
([cluster.md](cluster.md) §4.7). A and B have separate capacities, and dividing
by `banks` is what keeps the array working through a fill: the sweep for chunk
*i* is still reading while chunk *i+1* lands. Anything larger than one tile is a
loop over it, and `kernel.py` is that loop.

> **Both terms divide, and for a session only one of them did.** The rule used
> to read `nk = min(l1_a // (banks*gm), l1_b // gn, k_blocks)` — B sized against
> the *whole* of L1. At `l1_b = 512` and `gn = 32` that plans a 288-entry chunk,
> whose last offset is 287 against an 8-bit field, and the sub-tiles past the
> wrap read another K block's B. On silicon `64x288x256` came back with a worst
> element of **8.23e+02** and `64x320x320` with **3.49e+03**, 5,265 of 20,480
> elements past 10%, while `64x256x256` beside it was exactly right.
> [cluster.md](cluster.md) §4.6 has the full table and why it is the *product*
> `gn*nk`, not capacity, that predicts every case.
>
> `L1_OFF_SPAN` is the separate ceiling an 8-bit field imposes whatever L1 grows
> to, so it is a named constant rather than folded into `l1_b`. `ktpu.hw.kernel`
> and `ktpu.passes.tile` had the identical defect and are fixed identically;
> `test_a_chunk_never_outgrows_one_l1_bank` is parametrised over l1 =
> 256/512/1024 so the constant cannot be lost again. Every previously-passing
> shape keeps its exact tile — nothing regressed to buy this.

`choose_tile` ranks by **arithmetic intensity, discounted by padding**:

```python
score = 2 * gm * gn / (gm + gn)          # MACs per operand byte
score *= (m*n*k) / (padded_m * padded_n * padded_k)
key = (round(score * 4096), nk, -abs(gm - gn))
```

Intensity first because it is the quantity every other cost divides into — it
decides how many bytes the fetch path must move per unit of compute, and no
amount of scheduling changes it. Then K per fill, since operands are re-read
every pass and only the output stays put; then squareness, to break ties toward
the smaller padding bill.

> **Residency is the constraint, not the objective.** Maximising `gm*gn` — which
> is what this used to do — is a different thing: 32×1 and 8×4 both hold 32
> sub-tiles, and their intensities are 1.94 and 5.33.

Two guards on top of that, and both exist because the largest tile is not the
best tile:

* **Powers of two only.** Ranked on intensity alone the best shape at 512
  sub-tiles is 22×23 (22.5 MACs/byte against 16×32's 21.3), whose output block
  is 88×92 — so every dimension of every problem pads up to a multiple of 88 or
  92, and a 1024-cube pays 11% before any efficiency is counted. The intensity
  difference is 5%; the padding difference is not.
* **The padding discount itself.** At a 64×128 block a 300×300 GEMM pads to
  320×384 and does 36.5% more arithmetic than the problem contains. On shapes
  that already fit, the discount is 1 and the ranking reduces to plain intensity,
  which is why the 256-cube and 1024-cube answers do not move.

At `TILES=512, GA=128, GB=256` the answer is `gm=16, gn=32, nk=4` — a
**64×128×128** pass.

Every dimension is padded up to whole tiles before planning. Partial tiles would
make each pass a different size and buy nothing: the padding is zeros, a zero
contributes nothing to a dot product, and it does not affect a block's scale
either.

## 2. K is innermost

For a fixed output tile, the driver loops over K chunks:

```python
for mo in range(m_tiles):
    for no in range(n_tiles):
        for ko in range(chunks):          # <- innermost
            FILL A ; FILL B ; GEMM(acc = ko > 0)
        DRAIN                              # once, after the whole K sweep
```

The accumulator keeps the output tile resident across the entire K sweep and
writes it to memory **once**. Any other order spills a partial tile per K chunk
and reads it back, costing `M*N*(K/Kc)` of write traffic instead of `M*N`, plus
the same again in reads. At K=4096 with a 128-element chunk that is 32x in each
direction.

`GEMM.acc` is what makes the good order expressible at all — without it every
`GEMM` starts with `OP_LOAD` and overwrites the tile, so an output tile could
only ever come from one instruction. See [cluster.md](cluster.md) §4.2.

> Two different loops both mention K and they nest the opposite way. **Across
> chunks** (here) K is innermost, so the output tile stays resident. **Within one
> `GEMM` instruction** the K-block sweep is outermost over sub-tiles, so a tile
> address recurs every `gm*gn` cycles and the accumulator can be one bank
> ([acu.md](acu.md) §6). They are different levels and both are load-bearing.

## 3. Operands are stored tile-major

A pass needs the L1 entries for one `(output tile, K chunk)`: groups
`[t*gt, (t+1)*gt)` crossed with K blocks `[c*bc, (c+1)*bc)`.

In the natural group-major order — which is what `entry_index(group, kblock, nk)
= group*nk + kblock` gives, and what the manager's sweep reads — those are `gt`
separate runs of `bc` entries. A pass would need `gt` `FILL` instructions
instead of one, or the hardware would need a strided fetch.

Reordering memory removes the problem instead of paying for it:

```
   (group, lane, block, k)  ->  (tile, chunk, group, block, lane, k)
```

which is exactly the order a pass consumes, so a pass's entries become one
contiguous run and a `FILL` is a single instruction. Each entry still appears
exactly once; only the order changes, and the driver owns the order. **Layout is
part of the kernel, not a property of the tensor.**

`to_fp16_words_tiled` does it as a numpy transpose, not a Python loop — a 512x512
operand is 16k entries and the loop version takes minutes rather than
milliseconds. `to_fp16_words` is the same function with one group per tile and
one block per chunk, so there is only one packer to get right.

The layout contract itself is `[lanes][K]` row-major FP16 with `lanes % 4 == 0`
and `K % 32 == 0`, where a lane is a row of A or a **column** of B — B is stored
transposed. Two consequences worth knowing: `A @ B.T` is what `torch.nn.Linear`
already computes and its `weight` is `[N][K]`, so weights upload verbatim; and
`C[M][N]` row-major is exactly the shape the next layer wants as its A operand.

C is written per output tile: `gm*gn` words, one per 4x4 sub-tile, in the
manager's sweep order. `bench.read_result` walks tiles the same way the kernel
emitted them.

## 4. Rounds

A large GEMM has more passes than the machine can hold at once, so passes are cut
into **rounds** — each a self-contained upload / load / `GO`. The card never
needs the whole program, and nothing about the result depends on where the cuts
fall.

A round is bounded by **three** limits:

| limit | value | source |
|---|---|---|
| staging window | 128 flits | `STAGE_FLITS` in the agent |
| command RAM | 128 commands | `NCMD` in `main_orch` |
| passes per round | `INST_DEPTH - 1` = 31 | dispatch credit — [agent.md](agent.md) §6.1 |

Whichever binds first is the one that matters, and checking only one is how a
program silently overruns the resource it was supposed to fit. All three failure
modes are silent in hardware — see [agent.md](agent.md) §3 and §6.1, and
[orchestrator.md](orchestrator.md) §5.

The credit bound is the least obvious of the three. Each program permanently
consumes one credit, because its last instruction retires as
`SIG_BATCH_COMPLETE` and only `SIG_INST_COMPLETE` refills — so a round of `P`
programs seeded once with `C` credits needs `C > P`, and `C` is itself capped at
the CU's instruction-FIFO depth. Being cut here costs an extra round; being
wrong here stops the machine with nothing executed and no error.

The command cost of a pass is **not a constant**, because a kick only writes the
dispatch registers that changed since the last one. So the cut asks the program
builder rather than assuming a figure:

```python
fits_stage  = cur_flits + len(p.flits) <= stage_flits
fits_cmd    = len(control(cur + [p])) <= ncmd
fits_credit = len(cur) + 1 <= dev.INST_DEPTH - 1
```

`control` is used both to measure a candidate round and to build the real one, so
the two can never disagree about what a round costs. Guessing high wastes command
RAM; guessing low overruns it.

### 4.1 Clusters are interleaved before cutting

Rounds are cut from the pass list in order, so emitting one cluster's passes and
then the next would fill whole rounds with a single cluster's work and leave the
others idle through it. N clusters would take N times as long as one — exactly
what the kick-all-then-wait-once dispatch exists to avoid.

The list is round-robined across clusters first, with the ragged tail appended,
so every round is balanced no matter where the cuts land.

### 4.2 What a round's control program looks like

```
   WR   SIG_DONE, 0
   WR   PROG_CRED, INST_DEPTH            once per round, before any kick
   for each pass:   POLL PROG_STAT ; WR (changed registers) ; WR PROG_KICK
   POLL SIG_DONE, total flits
   DONE 0xC0DE
```

Every pass is kicked before any is awaited, and completion is one count for the
whole machine rather than a poll per cluster.

> **Credit is seeded once per round, not once per kick** — `control` calls
> `seed_credits(INST_DEPTH)` before the loop and `kick` writes no `PROG_CRED` at
> all. Re-seeding per kick makes the `C > P` arithmetic hold trivially, and it
> is wrong: credit is also the bound that keeps instructions in flight below the
> target's `INST_DEPTH`, so `P` kicks would admit `P * C` instructions against a
> FIFO of 32. Both halves are needed — the seed satisfies `C > P`, and the
> credit round-cut above keeps it satisfied. See [agent.md](agent.md) §6.1 for
> what re-seeding cost when it was tried.

## 5. The `wr_setup` shadow

`Program` remembers what it has already written to each register and skips a
write that would re-state the current value.

```python
def wr_setup(self, addr, data):
    if self._shadow.get(addr) == data:
        return self
    return self.wr(addr, data)
```

This is where the command RAM is actually won. Dispatching a pass writes four
registers, but across a round of passes only `DST` and `BASE` really move —
`DST` alternates between clusters and `BASE` advances, while `LEN` almost never
changes. Dropping the repeats roughly triples the passes a round can carry.

**Only for registers the hardware reads and never modifies.** `DST`, `BASE` and
`LEN` qualify: the dispatcher copies them into working counters on the kick and
leaves the registers alone, so the shadow stays true. Two kinds do not, and both
fail silently:

* one where the write itself is the event — `PROG_KICK`. Shadowing it would drop
  every dispatch after the first, and the agent ignores a kick it never received
  just as thoroughly as one it received while busy.
* one the hardware **consumes** — `PROG_CRED`. The shadow stops matching the real
  value the moment the machine runs, so skipping the write skips a real update.

The test is not "does the driver want the same value again". It is "does the
register still hold what the driver last wrote" — see [agent.md](agent.md) §7.

The shadow starts **empty**, so a program never assumes a value it did not set
itself. Rounds run as separate programs and the first kick of each writes the
full set. That is what makes rounds independent: no round depends on register
state another round left behind, so a round can be re-run or reordered without
changing the result.

## 6. Setup and program are separate

`Program.setup` holds `(addr, data)` pairs the host writes directly — the
instruction flits, straight into the staging RAM. `Program.cmds` holds only
control.

The program is not a recording of every AXI write. Putting flits in the command
RAM makes the host ship each one twice, once into the command RAM and again when
the command RAM replays it into the staging RAM, and makes the program grow with
the *problem* rather than with its *control flow*. On a two-cluster GEMM that is
55 commands down to 15, and the gap widens with every cluster.

`bench.write_files` emits them as separate files with a `rounds.hex` index
recording `(nsetup, ncmd)` per round, so the bench knows where to cut. A round
with zero commands is the end marker.

## 7. Checks the driver performs

The bench's capacities are compile-time constants of `mag_driver_tb.v`. A shape
that exceeds one of them does not fail loudly in simulation — it silently
computes something else — so the driver checks rather than trusting the caller:

| check | limit |
|---|---|
| minimum shape | 4 x 4 x 32 |
| memory image | `WORDS = 262144` (8 MB) |
| commands per round | `NCMD = 128`, raises |
| setup writes, all rounds | `MAX_SETUP = 1 << 18` |

`bench.WORDS` and `mag_driver_tb`'s `RAM_WORDS` must be the same number, or the
driver lays A, B and C out over a RAM that is a different size and they silently
overlap. Both are 262,144.

Everything that used to be a hard limit — output sub-tiles, L1 entries, program
length — is now handled by tiling and streaming, so it bounds the number of
passes rather than the problem.
