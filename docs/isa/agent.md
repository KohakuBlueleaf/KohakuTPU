# The agent dispatch registers

The register block inside MAG that turns staged flits into instructions running
on a CU.

Source: [`src/kohakunoc/noc_orchestrator.v`](../../src/kohakunoc/noc_orchestrator.v)
(the agent itself), [`src/kohakumas/mag.v`](../../src/kohakumas/mag.v) (which
instantiates it, wires the control AXI window to it, and shares its NoC ports
with it),
[`driver/src/kohakutpu/device.py`](../../driver/src/kohakutpu/device.py).

---

## 1. Why the agent is not a master

The agent never fetches from DRAM. The host stages instruction flits into a
local RAM through the same AXI slave it uses for the registers, then names a
destination and kicks. So there is no AXI master here, no address translation on
the dispatch path, and no ordering question between fetching an instruction and
the memory traffic that instruction will generate.

The cost is that the staging window bounds how much can be in flight, which is
what forces rounds ([kernel.md](kernel.md) §4). The alternative — a fetching
dispatcher — buys unbounded programs and pays for it with a second master on the
memory system, competing with the CUs it is feeding.

### 1.1 The agent has no NoC node of its own

It **shares MAG's ports**. `mag.v` presents `MEM_PORTS` mesh attachments and no
others; `agt_in_*` / `agt_out_*` are gone from its port list and the `AGT_X` /
`AGT_Y` parameters are deleted. Those attachments are **MAG ports, not memory
ports** — each carries operand traffic and this control traffic on the same
wires, one per mesh row on the west edge.

```
   inbound    each port demuxes by flit TYPE -- MEM_RD_REQ / MEM_WR_REQ /
              MEM_WR_DATA to that port's memory engine, EVERYTHING ELSE to
              the agent. The ports round-robin into the agent's single input;
              an ungranted port holds `busy` and its sender retries.

   outbound   a flit for row y leaves from the port that sits on row y, so
              dispatch spreads across every attachment instead of queueing
              behind one link. The agent WINS this arbitration; the memory
              engine holds, which it already does under link backpressure.
```

**The agent answers at port 0's coordinate, `(MEM_X, MEM_Y)`** — `ORC_X` and
`ORC_Y` are tied to it. That is what makes §4's reply path work with no extra
mechanism: a CU replying to the source of its `CU_INST` addresses that node, the
flit lands at port 0, and the demux hands it to the agent because `CU_SIGNAL` is
not a memory type. **One address, two consumers, told apart by what the flit is
rather than by where it went.**

Nothing in the register map, the credit scheme or the dispatch sequence changes.
What changes is that dispatch is no longer funnelled through a single link, which
is what the eight-cluster case is short of. The reasoning and the measurement are
in [`../mas/spec.md`](../mas/spec.md) §2.5.

## 2. Register map

All offsets are relative to `MAG_BASE = 0x1000_0000`. The window is 64 bits
wide by design: it carries control, never bulk data.

| offset | name | access | contents |
|---|---|---|---|
| `0x0040` | `PROG_DST` | RW | `{y,x}` of the target node, 8 bits |
| `0x0048` | `PROG_LEN` | RW | flits in this program, 16 bits |
| `0x0050` | `PROG_KICK` | W | any write starts the dispatcher |
| `0x0058` | `PROG_STAT` | R | `[0]` run, `[16:1]` flits left, `[32:17]` credit |
| `0x0060` | `PROG_CRED` | RW | seed the dispatch credit counter, 16 bits |
| `0x0068` | `PROG_BASE` | RW | first staging slot of this program, 16 bits |
| `0x0070` | `SIG_DONE` | R | completions from every node, 16 bits |
| | | W | any write clears the counter |
| `0x1000` | `NODE_STATUS` | R | `+ {y,x}*8` — see §5 |
| `0x2000` | `STAGE` | W | instruction flits, 5 x 64-bit words each |

The agent also carries `CTRL`, `CAPS`, `IRQ_*` and the raw-flit mailbox
(`TX_FLIT0` at `0x0100`, `RX_FLIT0` at `0x0180`). The mailbox exists for
bring-up — it can inject any flit, including a malformed one, which an
address-mapped bridge could never do. The driver does not use it and it is
ignored while a dispatch is running, because it shares the TX FIFO.

> `device.py` comments `PROG_STAT` as "`[0]` run, `[16]` .. credit". The RTL
> packs `{prog_credit, prog_left, prog_run}`, so credit is at `[32:17]` and
> `prog_left` occupies `[16:1]`. Only bit 0 is ever polled, so nothing depends
> on the wrong comment — but a reader diagnosing a stall from `PROG_STAT` will
> read the wrong field.

### 2.1 The `{y,x}` node index

`PROG_DST` and the `NODE_STATUS` index are both one byte: `(y << 4) | x`.

```python
def node_index(x, y):
    return ((y & 0xF) << 4) | (x & 0xF)
```

The dispatcher splits it back the same way — `dst_x = prog_dst[POS_WIDTH-1:0]`,
`dst_y = prog_dst[2*POS_WIDTH-1:POS_WIDTH]`. The nibble packing in the driver is
only correct because `POS_WIDTH == 4`; a wider mesh coordinate changes the
encoding on both sides.

Y in the high half is not arbitrary. `NODE_STATUS` is indexed by the same byte,
so a row of the mesh is a contiguous run of status words, and there is one slot
for every coordinate including border PEs — no compaction table, no holes.

## 3. Staging

`STAGE` is a flat array of 64-bit words. Flit `n` word `w` is at
`0x2000 + (n*5 + w)*8`, least significant word first.

288 bits do not divide into 64, so a flit is 5 words with the top 32 bits of the
last word unused. Padding to a power of two would waste a fifth of the RAM to
save one multiply in an address the host computes anyway.

| limit | value |
|---|---|
| `STAGE_FLITS` | **128** |
| staging words | 640 |
| window | `0x2000`–`0x33FF` |

**What happens when it is exceeded.** The window end is derived from
`STAGE_WORDS`, so a write past flit 127 falls out of `is_stage`, drops into the
register decode, matches nothing and is discarded. The program then dispatches
whatever those slots held before. Nothing reports it.

That decode used to be `waddr[15:12] == 4'h2`, i.e. a single 4 KB page = 512
words = **102.4 flits**, while the RAM was sized for 128. Flits 103 onward were
silently discarded. The symptom was a program that stopped at exactly 51 of 64
sub-tiles with `run=1 left=10 credit=0`, which reads like credit exhaustion —
re-running with 4 credits instead of 16 stopped at 51 again, and that is what
ruled flow control out. A rate problem moves; a decode boundary does not. See
[`../system.md`](../system.md).

### 3.1 `PROG_BASE`

`PROG_BASE` is the first staging *slot*, in flits. The dispatcher starts reading
at `prog_base * 5`.

Without it every kick restarts at slot 0, so a second cluster's flits cannot be
staged until the first has consumed its own — which forces a wait between
dispatches and serialises clusters that have no data dependency at all. With it,
N programs live in the window at once and all N run concurrently. Measured on
`C[32,32]` across two clusters:

```
   serial       7928 ns    overlap    0 cycles
   concurrent   4032 ns    overlap  860 cycles     1.97x
```

## 4. Dispatch and credits

A kick is honoured only while `prog_run` is low. **A kick during a dispatch is
silently ignored**, which is why the driver polls `PROG_STAT` before every one —
a dropped kick has no error path, and the symptom is a cluster that simply never
reports done.

The dispatcher reads 5 words per flit, rewrites the routing header, and pushes.
It stamps `dst` from `PROG_DST` — so one staged program can be sent to any
node — and `src` with the agent's own coordinates, which are port 0's (§1.1), so
the target can reply without being configured with the agent's address. The
mailbox path deliberately does neither; injecting a hand-built header is the
point of it.

Which port a dispatched flit physically leaves from is decided by its
**destination row**, not by `src`: `PROG_DST` selects the outbound attachment,
so a round that kicks programs at several rows spreads across several links.

It stalls on **credit**, never on the network. Backpressuring `CU_INST` into the
mesh is the protocol deadlock that credits exist to prevent: the instruction
that cannot be delivered blocks the flit behind it, which may be the completion
signal that would have freed the resource.

```
   PROG_CRED write   credit = value                (a host write always wins)
   flit dispatched   credit -= 1
   SIG_INST_COMPLETE credit += 1
   credit == 0       dispatcher stalls
```

Credit holds instructions in flight below the CU's instruction FIFO depth,
`INST_DEPTH = 32` in `noc_cu_base`.

> **The last flit of a program does not return its credit.** The refill
> condition is signal code `0x00` (`SIG_INST_COMPLETE`) only. A flit with the
> header `last` bit set retires as `SIG_BATCH_COMPLETE` (`0x01`), which the
> agent counts in `SIG_DONE` but does not credit. Every kicked program therefore
> consumes one credit permanently, which is why the seed is re-stated before
> every kick rather than shadowed — §6.1.

## 5. `NODE_STATUS`

One 64-bit word per coordinate, written from every `CU_SIGNAL` that arrives.

| bits | field |
|---|---|
| `[63:56]` | signal code of the most recent signal |
| `[55:24]` | that signal's 32-bit argument |
| `[23:8]` | signal **count** for this node |
| `[7:1]` | zero |
| `[0]` | valid — this node has signalled at least once |

A count, not a sticky flag: a host polling slower than events arrive can tell how
many it missed.

`CU_SIGNAL` is summarised here and deliberately **not** queued into the RX FIFO.
Queuing it would let unread signals fill the FIFO, raise `noc_in_busy`, and stop
the agent accepting anything — including the signals that return dispatch
credits. A host that never drained RX would wedge the control plane silently
after 16 completions.

`await_node` polls `(nflits << 8) | 1` under mask `0x00FF_FF01`.

## 6. `SIG_DONE` — one poll for the whole machine

`SIG_DONE` counts **every** signal from **every** node, in one 16-bit register.

Waiting per node costs one command per node, so the control program grows with
the machine for a question — "is everyone finished" — whose answer is a single
number. And the program is what the host must push over AXI before anything
runs. One counter makes that one poll however many clusters there are.

**It counts every signal type, not just `SIG_INST_COMPLETE`.** This is the point
worth being explicit about. `noc_cu_base` reports the last instruction of a
program as `SIG_BATCH_COMPLETE` (`0x01`), not `SIG_INST_COMPLETE` (`0x00`); a
fault reports `SIG_FAULT` (`0x04`). A counter that filtered on `INST_COMPLETE`
would see N-1 of every N instructions, and a host waiting for N would wait
forever. The register counts `in_is_signal`, which is the type field, so the
expected total is simply the number of dispatched flits.

Writing `SIG_DONE` clears it, regardless of the data written. The driver writes
zero for readability; any value does the same thing.

The counter is 16 bits and wraps. A single program is bounded by the staging
window at 128 flits, so wrap can only happen if a host clears less often than
once per 65,536 dispatched instructions. The driver clears once per round.

### 6.1 Why `PROG_CRED` is never shadowed

`prog_credit` is a counter the hardware **consumes**, not a value it merely
reads. A register like that may never be shadowed by the driver, and the credit
arithmetic is what makes the rule concrete.

Combining §4 and §6: a round that kicks `P` programs totalling `F` flits issues
`F` dispatches and gets back only `F - P` credits, because `P` of the
completions are `SIG_BATCH_COMPLETE` and only `SIG_INST_COMPLETE` refills.
Seeded **once** with `C` credits, the dispatcher can push at most `C + (F - P)`
flits, so a round seeded once needs

```
   C > P
```

Seeding once lets the deficit accumulate across the whole round, and nothing
bounds `P` by `C`: `kernel.plan` cuts rounds against the staging window and the
command RAM, so a 13-flit pass gives 9 passes per round against a default
`C = 8`. The dispatcher then stalls with `prog_run` still set, the `PROG_STAT`
poll never completes, and the round wedges with no error anywhere.

There are two ways to satisfy `C > P`, and **only one of them is correct**.

*Re-seeding before every kick* makes the arithmetic hold trivially at `P = 1`.
It is also wrong, and it was tried: credit is not merely anti-starvation
bookkeeping, it is the bound that keeps instructions in flight below the
target's `INST_DEPTH` (§4). Re-seeding per kick lets `P` kicks admit `P * C`
instructions against a FIFO of 32. A full instruction FIFO raises
`noc_in_busy`, which backpressures the mesh link, which also blocks the
**memory read responses the CU is waiting on** — so it can never drain the FIFO
that is blocking it. The machine stops having executed nothing, with no error.
The symptom scales with passes *per cluster*, so it hides at small sizes and at
wide cluster counts, and strikes when passes pile onto one cluster.

So the driver **seeds once per round** (`kernel.control`), with
`C = INST_DEPTH`, and `kernel.plan` bounds `P <= INST_DEPTH - 1` as a third
round-cutting constraint alongside the staging window and the command RAM. Both
halves are required: the seed satisfies `C > P`, and the round cut guarantees it
stays satisfied.

**The general rule: a register the hardware modifies must never be shadowed.**
A shadow records what the driver last *wrote*, not what the register now
*holds*. But "not shadowed" does not imply "written every time" — for a register
that is also a *bound*, re-writing it destroys the bound. `PROG_CRED` is
written exactly once per round: not shadowed, not repeated. Only pure latched
configuration may be shadowed — see §7.

## 7. What a kick costs

```
   -- once per ROUND, before any kick --
   WR   SIG_DONE    clear the completion counter
   WR   PROG_CRED   seed the round's credit; see §6.1

   -- per kick --
   POLL PROG_STAT   want run == 0
   WR   PROG_DST    only if it changed
   WR   PROG_BASE   only if it changed
   WR   PROG_LEN    only if it changed
   WR   PROG_KICK   ALWAYS -- the write is the event, not the value
```

The dispatch registers are three different kinds, and only the first may be
shadowed:

| kind | registers | why |
|---|---|---|
| latched configuration | `PROG_DST`, `PROG_BASE`, `PROG_LEN` | the dispatcher copies them into working counters on the kick (304–305) and never writes them back, so the register still holds what the driver last wrote |
| consumed counter | `PROG_CRED` | the hardware decrements and refills it (296, 298), so the driver's shadow is stale the moment the machine runs — §6.1 |
| event register | `PROG_KICK` | the write *is* the launch; the value carries nothing |

The test is not "does the driver want the same value again". It is **"does the
register still hold what the driver last wrote"** — and for the last two kinds
the answer is no, or the question is meaningless. Both failures are silent: a
shadowed kick is a dispatch that never happens, and a shadowed credit is a
dispatcher that stalls mid-round with `prog_run` stuck high.

See [kernel.md](kernel.md) §5.
