---
title: Writing a compute unit
summary: The unit is entirely yours — datapath, memories, pipeline, instruction semantics. This is how it connects, and what the two production units did that made connecting easy.
tags:
  - integrate
  - compute-unit
  - guide
---

# Writing a compute unit

**The compute unit is yours.** Its datapath, its memories — how many, how wide,
which primitive, what read latency — its pipeline depth, what its instructions
mean. The framework has no template for any of that, and the evidence is that the
two production units in the reference project share almost none of it
([what-you-own.md](what-you-own.md) §3).

What the framework removes is the **connection** problem. How to be a node on the
mesh. How to be dispatched to. How to ask for memory and receive it. How to report
completion so a credit comes back. How to be found by a driver that has never
heard of you. That work is identical for every accelerator anyone would build
here, it is where the silent failures live, and it is done.

This page walks that connection, and marks the conventions that make it painless.
The normative contract is
[spec/compute-unit-port.md](../spec/compute-unit-port.md) — this is the guide,
that is the law.

---

## 1. The port you present

The mesh sees six signals and a clock. Name them exactly this, because the mesh
generator connects by name:

```verilog
    input  wire                   clk,
    input  wire                   resetn,

    input  wire [FLIT_WIDTH-1:0]  noc_in_data,
    input  wire                   noc_in_valid,
    output wire                   noc_in_busy,
    output wire [FLIT_WIDTH-1:0]  noc_out_data,
    output wire                   noc_out_valid,
    input  wire                   noc_out_busy,
```

Both production units present exactly this
(`src/kohakutpu/matmul/mx_cluster_cu.v`, `src/kohakutpu/vector/vec_cu.v`), as
does `src/kohakuaccel/noc/endpoint/noc_cu_null.v` (§1.2). Anything else
in your port list is yours — debug counters, status bits — and the generated top
wires them out.

The parameters the framework expects to set on you:

| parameter | what it is |
|---|---|
| `FLIT_WIDTH`, `POS_WIDTH` | network geometry. Take them, pass them down, never hardcode |
| `POS_X`, `POS_Y` | **your** coordinates. Stamped into every flit you send |
| `MEM_X`, `MEM_Y` | the memory agent port you should address, chosen for you by hop count |
| `INST_DEPTH`, `RECV_DEPTH` | queue depths, chosen at ship level |

`POS_X`/`POS_Y` are not a preference — the router at that coordinate is the one
whose local port you attach to, and a flit sent with a different source is
answered to the wrong place.

> `mx_cluster_cu` names its coordinates `CU_X`/`CU_Y` instead, and the mesh
> generator carries a special case for it. That is a divergence, not an
> alternative. Use `POS_X`/`POS_Y`.

### 1.1 Instantiate the port module

`src/kohakuaccel/noc/endpoint/noc_cu_base.v` holds the mesh-facing side so your unit conforms by
construction:

```verilog
    wire [FLIT_WIDTH-1:0] inst_flit, recv_flit;
    wire                  inst_valid, recv_valid, send_ready;
    reg                   inst_ready, recv_ready;
    reg                   exec_done, exec_fault;
    reg  [31:0]           exec_result;
    reg  [FLIT_WIDTH-1:0] send_flit;
    reg                   send_valid;

    noc_cu_base #(
        .FLIT_WIDTH(FLIT_WIDTH), .POS_WIDTH(POS_WIDTH),
        .POS_X(POS_X), .POS_Y(POS_Y),
        .CU_TYPE(16'h1234), .CU_VERSION(8'h01), .N_BUFFERS(2),
        .INST_DEPTH(INST_DEPTH), .RECV_DEPTH(RECV_DEPTH)
    ) u_base (
        .clk(clk), .resetn(resetn),
        .noc_in_data(noc_in_data),   .noc_in_valid(noc_in_valid),
        .noc_in_busy(noc_in_busy),
        .noc_out_data(noc_out_data), .noc_out_valid(noc_out_valid),
        .noc_out_busy(noc_out_busy),
        .inst_flit(inst_flit), .inst_valid(inst_valid), .inst_ready(inst_ready),
        .exec_done(exec_done), .exec_result(exec_result), .exec_fault(exec_fault),
        .dbg_ctr(64'd0),
        .send_flit(send_flit), .send_valid(send_valid), .send_ready(send_ready),
        .recv_flit(recv_flit), .recv_valid(recv_valid), .recv_ready(recv_ready),
        .inst_space(), .busy()
    );
```

Name the instance **`u_base`** — the port-protocol bench reaches into it by
hierarchical path to check things no port can show
([conformance.md](conformance.md) §4).

A unit that never originates traffic ties `send_valid` low. A unit that expects
nothing but instructions ties `recv_ready` high so the receive queue self-drains.
**Do not tie `recv_ready` low**: the queue fills, `noc_in_busy` sticks high, and
your router stops accepting anything at all.

`CU_TYPE` is the 16-bit identifier published so a host can enumerate a unit it
has never heard of. `CU_VERSION` is used as a mesh-wide build number in practice
— bumped in every endpoint whenever any ISA or datapath changes, so a driver can
ask "is this the bitstream my compiler targets" and get a truthful answer.

### 1.2 What `noc_cu_null.v` is, and is not

It comes up as soon as anyone looks for a starting point, so be clear about it.

**It is a measurement instrument.** `src/kohakuaccel/noc/endpoint/noc_cu_null.v` is the smallest
thing that is a legal node — every framework obligation, no arithmetic — and it
exists so that "what does attaching an endpoint to the mesh cost, before any
compute" is a real number. It is instantiated only by
`src/kohakuaccel/verif/noc_tile_1r.v` and `src/kohakuaccel/verif/noc_cluster_2x2.v`, both
measurement-only harnesses.

**It is not a template**, and copying it will teach you the wrong things:

- It is built to defeat synthesis pruning. Every bit of both inbound flits is
  folded into an output that reaches a port, and its traffic originates from
  external stimulus so the mesh cannot be proven idle and constant-folded. That
  XOR tree is a measurement artefact, not a datapath idiom.
- It retires in the cycle it accepts, which is the one thing a real unit should
  never look like.
- **It carries a live bug.** Its local type constant for unit-to-unit data is
  `4'h4`, which is the code for memory write data; the correct value is `4'h8`.
  It builds real flits with the wrong one. Nothing has broken because those
  harnesses contain no memory agent to be confused by it, but this is exactly the
  divergence that has bitten this codebase once already, in the module a
  newcomer is most likely to copy.

**There is no starting-point unit today, and there probably should be** — a
small, correct, deliberately boring unit that fetches something, computes
something trivial and drains it, wired into the bench registry. Until one exists,
the honest advice is: read `noc_cu_null.v` for the *shape* of a conforming node,
and read one of the two production units for how a real one is built.

---

## 2. What the port module does for you

Re-implementing any of this is a bug, not a preference.

**Framing.** Instructions are separated from everything else by flit type on
arrival, into their own queue. You never parse a header to find an instruction.

**Reply addressing.** When you accept an instruction, the framework latches its
source, its transaction id and its batch marker, and sends your completion back
to whoever sent it. Your unit is never configured with its orchestrator's
address, and never reconfigured when the orchestrator moves.

**Discovery.** Control reads are answered inside the port module and never reach
you: capabilities, status, a framework-owned counter pair, and one 64-bit word of
your own. They are answered while your datapath is busy or faulted, which is the
point — it is how software finds out that you are.

**Cycle counting.** Busy cycles and retired instructions, counted identically for
every unit type. Host wall-clock cannot substitute: one control-plane access
dwarfs the work being timed.

**Completion queueing.** Completions queue rather than overwrite. A completion
returns a dispatch credit, so a lost one stalls the machine later with nothing to
point at. When the queue fills, issue stops — the framework will not execute
something it cannot report.

**Transmit arbitration.** Completions beat control replies beat your traffic. Do
not build a second arbiter in front of it.

The one hook that is yours here is `dbg_ctr`: 64 bits, published as a control
register, conventionally two 32-bit counters. Spend it on the number that makes a
disappointing measurement *attributable*. KohakuTPU's cluster reports compute
cycles against memory-wait cycles, which is the difference between "it was slow"
and "it was waiting"; its vector core reports the last kernel's cycle count. The
two disagree about what the register means and about whether it is cumulative,
and that is correct — the driver decodes it per unit type.

---

## 3. Receiving instructions

```
    inst_flit   [FLIT_WIDTH-1:0]   the whole flit; your bits are in the payload
    inst_valid                     one is available            (framework drives)
    inst_ready                     you took it                 (you drive)
```

Issue is strictly one at a time: `inst_valid` stays low from the cycle you accept
one until you report `exec_done`. If your datapath is pipelined and you want
several in flight, buffer them behind your own `inst_ready` and retire in order —
the framework tracks exactly one reply context.

Decode straight out of `inst_flit`; it is a queue output and holds until you take
it:

```verilog
    wire [3:0]  i_op   = inst_flit[255 -: 4];
    wire [33:0] i_addr = inst_flit[251 -: 34];
    wire [15:0] i_n    = inst_flit[217 -: 16];
```

Note the convention: the header sits at the top of the flit and the payload
below, so both production units decode instruction fields downward from an
absolute bit position while extracting header fields relative to `FLIT_WIDTH`.
Keep the two styles apart — an instruction field written relative to `FLIT_WIDTH`
moves if the coordinate width ever changes, which is not what you meant.

The accept pattern both units use:

```verilog
    always @(posedge clk) begin
        inst_ready <= 1'b0;            // default: one-cycle pulse
        ...
        if (inst_valid && !inst_ready) begin
            // latch what you need out of inst_flit
            inst_ready <= 1'b1;
        end
    end
```

The `!inst_ready` guard matters. `inst_ready` is registered, so it is still high
on the cycle the framework consumes it; an accept condition that does not exclude
it fires a second time against the same instruction and re-latches its operands
underneath you.

How to spend the payload bits — and which instruction sets already exist that you
should be *using* rather than inventing — is
[instruction-set.md](instruction-set.md).

---

## 4. Fetching operands

You fetch by sending the memory agent a request flit on `send_*` and receiving
responses on `recv_*`. Both are ordinary flits you build; the framework only
arbitrates the outbound port.

```
    send_flit / send_valid / send_ready    you originate
    recv_flit / recv_valid / recv_ready    everything addressed to you that is
                                           not an instruction and not a control read
```

Four idioms make this work, and the first three are **forced** — the memory agent
hands you data in this shape whatever you do:

**One descriptor names the whole run.** A read request carries a base address, a
count and layout flags, and the agent walks the address sequence itself. The
round trip is paid once per fetch, not once per word. Do not build a requester
that issues one request per entry and waits — `mx_cluster_cu` deleted its and
says so in a comment: a descriptor *removes* the requester rather than pipelining
it.

**The transaction id is your placement tag.** Put the destination slot in it; the
agent echoes it back on every response, so a response names its own placement and
your receiver needs no cursor of its own. This is what makes out-of-order or
interleaved arrival a non-event, and it is the single most useful idiom on this
page.

**Demultiplex by flit type, never by arrival position.** The routers interleave
whatever is in flight, so another sender's flit can land in the middle of your
burst. Both units switch on the type field of `recv_flit` and nothing else:

```verilog
    wire [3:0] rtype = recv_flit[FLIT_WIDTH-4*POS_WIDTH-1 -: 4];
```

**Hold a flit until it is taken.** `send_ready` is low whenever a completion or a
control reply is waiting, so it can be low for many cycles. Assert `send_valid`,
hold `send_flit` unchanged, and clear only on the cycle `send_valid &&
send_ready`. Withdrawing a flit because the port was busy destroys it, silently.

The request payload layout, the flag bits and the response format are
[spec/memory-protocol.md](../spec/memory-protocol.md).

---

## 5. Your memories

This is the part the framework does **not** provide, and it is worth being blunt
about, because "the framework gives you local memory" would be false.

The two production units, in one project, sharing none of it:

| | matmul cluster | vector core |
|---|---|---|
| operand memory width | 928 bits | 256 bits |
| how many | two — one per operand — plus a resident accumulator tile per node, plus a register file mirrored three ways for three read ports | one, plus an instruction memory |
| primitive | block RAM for operands, ultra RAM for the accumulator tile | block or ultra for the operand memory, distributed for the instruction memory |
| read latency | 1 for operands, 2 for the accumulator tile | 1 or 2, following the primitive — ultra cannot do 1 |
| entry assembly | four response words permuted into one 928-bit entry, committed on the last | a response word stored as it arrives |

If the framework had fixed any row of that table, one of these units could not
exist.

What it does give you is a set of hard-won conventions:

**Name the primitive; never infer it.** Instantiate through
`src/kohakuaccel/common/kohaku_sdpram.v` with an explicit `MEM_PRIM` and `READ_LAT`, never a
`reg` array left to synthesis. Inference makes both the resource cost *and the
read latency* depend on a tool heuristic, and read latency sets pipeline depth,
which is a design decision. Both units carry comments explaining what their
latency number is load-bearing for — one of them documents a two-cycle control
delay whose absence shifts every result by one sub-tile, structurally and
silently.

**Assemble wide entries with one register, and assert the assumption.** One
assembly register is sufficient *only* because a single server delivers an
entry's words consecutively. That is a property of the server, not of the
protocol: a second server, a reordering fetch engine, or two senders into one
unit would interleave two entries into one and produce a plausible wrong result.
Check it in simulation at the point of assembly, so the message names the module.

**Make the memory addressable, not ping-pong.** An instruction that retires on
issue lets the next fill land while the current computation reads — but only if
the instruction can say *where*. Hardware double-buffering gives you two regions,
no way to leave a third operand resident, and no way to express a reduction
longer than the memory.

**A bank bit beats widening a field.** When the cluster's memory doubled, the
offset fields stayed the same width and a bank bit was added above them, so every
instruction encoded before the change still addresses exactly what it did.

---

## 6. Returning results

Two destinations, and the choice is usually an instruction bit.

**To memory.** A descriptor flit followed by data flits, with the last one
flagged. Amortise: one descriptor over a burst costs the agent one transaction
instead of one per word. `mx_cluster_cu` collects results into a double-banked
buffer so collection and transmission overlap, and its burst length reduces
exactly to one beat per descriptor at its minimum — which is what makes it safe
to turn down while debugging.

**To another unit.** A descriptor flit naming a buffer, an offset and a length,
then that many data flits. The receiver acknowledges with a data-received signal
when the descriptor's flags ask — without it a sender that waits would wait
forever, because the framework's completion signalling covers *instructions*, and
a transfer is not an instruction.

Both units enforce two rules here with simulation assertions, because violating
either produces a plausible wrong answer rather than an error:

- **One stream at a time into one unit.** Compare each data flit's source against
  the open stream's and fault on a mismatch. Flits of one stream cannot arrive
  out of order — same source, same destination, same path — which is what makes a
  mismatch conclusive.
- **Range-check the descriptor, and still count the stream out.** An offset field
  is wider than the buffer it indexes, so an over-range burst wraps and
  overwrites. Reject the writes but keep counting the flits, or the next data
  flit is read as a descriptor and the damage spreads.

One more, easy to miss: **let the acknowledgement destination be redirectable.**
An acknowledgement that goes back to the sender is useless when the sender is
another unit — nothing there consumes it, and the host cannot sequence a reader
behind a writer. Both units accept an explicit acknowledgement node, with zero
meaning "the sender", which is unambiguous because a mesh corner can hold no
endpoint.

---

## 7. Saying you are finished

```
    exec_done                pulse: this instruction retired    (you drive)
    exec_result   [31:0]     whatever you want reported         (you drive)
    exec_fault               it failed instead                  (you drive)
```

One pulse per accepted instruction, exactly. The framework turns it into a signal
addressed to whoever sent the instruction, chooses the code — ordinary, batch, or
fault — and returns the credit.

**`exec_done` is one cycle.** It is an edge, not a level. Two cycles queue two
completions for one instruction and hand the orchestrator a credit it never
spent.

**Never raise `exec_done` in the same cycle as `inst_ready`.** The framework drops
its in-flight flag on `exec_done`, so the newly accepted instruction's completion
finds the flag low and is never queued. No unit does this, and the port bench
watches for it.

**`exec_result` must be combinational at the point you register it.** Reading
your own `exec_result` inside the block that assigns it gets the previous value,
because it is a non-blocking assignment. Compute into a combinational `reg` and
use that for both.

**Retire at the point that makes the report true.** This is the design decision
on the page, not a detail. `mx_cluster_cu` retires its sweep *on issue*, because
the pipeline needs nothing more from the sequencer and holding the instruction
would only stop the next fill; it retires its drain only once the last write has
left the unit, because a completion that arrives ahead of the memory traffic it
stands for is a lie the moment a later step reads what an earlier one wrote. Ask
what the host will do on hearing the completion, and retire when that becomes
safe.

**Faults are per instruction, not sticky.** Something detected asynchronously — a
malformed transfer arriving between instructions — should be latched, reported
once at the next instruction boundary, and cleared. A malformed burst is one
fault, not a unit that faults forever. Report a *code*: the fault argument
reaches the host verbatim, and a fault that says only "something" costs a
simulation run to localise.

---

## 8. Reset and backpressure

The obligations are in the spec (§2, §5, §6 there). What you need day to day:

**`resetn` is active low and synchronous.** Pass `!resetn` down to anything of
yours that wants active-high — both production datapaths take `rst` that way.

**Clear the right things.** Any pulse, any valid, any burst counter, any
"stream open" flag. A stream-open flag that survives reset makes the first
descriptor after reset look like a data flit. `recv_ready` should be low in reset
and high (or genuinely conditional) out of it.

**Hold `valid` until you see `busy` low.** The link is busy/valid with retry, not
valid/ready. Accept if and only if `valid && !busy`, never twice.

**Keep `busy` transient.** Refusing is legal; refusing forever is fatal. Your
`busy` must be tied to a queue something is actually emptying, and **nothing that
holds your receive path closed may require another inbound flit to clear it.**
That is the one rule whose violation deadlocks the mesh rather than your unit.

**Drain flit types you do not consume.** Anything addressed to you that is not an
instruction and not a control read lands in your receive queue — including write
acknowledgements nobody wants. Held, they wedge the instructions behind them. Two
dispositions, both in use:

- Accept and drop, with a default arm on your `recv_ready` decode that is `1'b1`
  for unknown types. `mx_cluster_cu` does this and prints a simulation message
  naming the type, because silent loss is the whole hazard of dropping.
- Filter ahead of the queue, for a type you never want:

```verilog
    wire in_ack = (in_ty == T_MEM_WR_ACK);
    noc_cu_base ... u_base (
        .noc_in_valid(noc_in_valid && !in_ack),
        .noc_in_busy(base_in_busy), ...);
    assign noc_in_busy = in_ack ? 1'b0 : base_in_busy;
```

  Cheaper than a queue slot, and it cannot fill.

**Decide `recv_ready` from flit type, not from your state alone.** A memory
response only makes sense inside a fetch, but a unit-to-unit transfer is
unsolicited by definition. `mx_cluster_cu` registered `recv_ready` on state and
silently discarded peer data during a fill; it is combinational and
type-dispatched now.

---

## 9. Traps that have cost real time

Each was found in simulation on this codebase.

**Concatenations must be exactly `FLIT_WIDTH` wide.** Verilog left-pads a short
one, shifting the header into the payload; the flit routes nowhere and nothing
reports it. Derive the pad from `FLIT_WIDTH` as a `localparam`.

**Unsized literals contribute 32 bits.** `(BASE + i*4) * 32` in a concatenation
is 32 bits wide, not the field width, and every field below it shifts.

**Declare every net.** An undeclared identifier in a port list becomes an
implicit one-bit net in Vivado, so a wide link silently becomes one wire and
elaborates cleanly. Open every file with `` `default_nettype none ``.

**One driver per register.** Two `always` blocks driving one signal simulate by
scheduling order and synthesise to something else.

**Variable part-select writes build a barrel mux** across the whole register.
Unroll over the varying term; `mx_cluster_cu` unrolls its operand placement over
the word index for exactly this reason.

**Serial loops synthesise serially.** A dependency chain across a wide vector
inside one pipeline stage is a deep LUT chain that no amount of pipelining
*around* it fixes. Restructure: smear-isolate-encode for searches,
mask-then-reduce for sticky bits.

**Paired parameters that must agree, with nothing checking them.** Derive one
from the other, or assert the relationship at elaboration.

**Decode derived control once, into a register.** A multiply of two instruction
fields feeding a state machine's clock enable became the worst path in a unit
here. If a control decision is an arithmetic function of instruction fields,
compute it when you latch the instruction.

**Check for `x` on addresses you send.** An `x` in an address is invisible
downstream and fatal: memory returns `x`, your datapath consumes it, and the
result is a plausible-looking zero, so the symptom points at the arithmetic. Put
the check at the producer under `` `ifndef SYNTHESIS ``, so the message names the
module that built the bad flit.

**In simulation, `glbl` holds global set/reset for the first 100 ns**, so
primitive-backed registers ignore your reset before then. Start stimulus after
that.

---

## 10. Four units to read

**Two production units. Both KohakuTPU — a project built on the framework, not
the framework.** They are here because they take deliberately different shapes,
which is how you tell a general port from one accidentally fitted to a single
style.

| | `src/kohakutpu/matmul/mx_cluster_cu.v` | `src/kohakutpu/vector/vec_cu.v` |
|---|---|---|
| what it is | a matmul cluster on one local port | a programmable vector core |
| instructions | three macro-ops: fill, sweep, drain | load instruction memory, set a descriptor, run |
| who decides an address | the compiler — a fetch is an instruction | the running kernel, and the unit wraps its requests into flits |
| retirement | on issue for the sweep, on last-write-out for the drain | when the kernel halts, reporting its cycle count |
| fault path | a malformed inbound stream, reported once at the next instruction boundary | a kernel fault, reported with its code |
| memory traffic | fetch by streaming descriptor, drain in bursts, unit-to-unit transfer | fetch, drain, and unit-to-unit transfer |

The instructive difference is the third row. One unit's operands are named by the
compiler and fetched by the sequencer; the other's are named by a program running
inside the unit, and the unit is a translator between its request port and the
mesh. Both are legal; pick by asking who knows the address.

**Two minimal units**, short enough to read in one sitting:
`tests/noc/cu_alu.v` consumes instructions with an opcode-dependent latency, so
retirement is out of step with issue; `tests/noc/cu_relay.v` originates traffic
and retires only once the network has taken the packet, so "complete" means
*sent*, not *queued*.

---

## 11. Before you call it done

- [ ] every issued instruction produces exactly one `exec_done`, a single cycle,
      never coinciding with `inst_ready`
- [ ] `send_valid` is held with `send_flit` unchanged until `send_ready`
- [ ] your receive path is never blocked on something only an inbound flit can
      clear, and unknown flit types are dropped rather than held
- [ ] every flit you build is exactly `FLIT_WIDTH` bits, with the pad derived
      from `FLIT_WIDTH`
- [ ] memories are named through `src/kohakuaccel/common/kohaku_sdpram.v` or
      `src/kohakuaccel/common/sync_fifo.v`, with explicit read latency
- [ ] `CU_TYPE` is unique, and `POS_X`/`POS_Y` match the router you hang off
- [ ] `dbg_ctr` reports something you would want during a disappointing
      measurement
- [ ] it retires everything with its outbound link held busy
- [ ] the port-protocol bench passes, then the mesh bench, then end to end
      ([conformance.md](conformance.md))

---

## 12. Open questions

- **There is no parameterisable conformance harness.** The port bench
  instantiates one unit by name and probes it hierarchically, so pointing it at a
  new unit means editing it. What it needs to be generic — an instance-name
  convention, a wrapper, a set of exported probes — is unsettled, and the tree
  currently spells the base instance two different ways. This is the most
  valuable missing piece of framework tooling.
- **Coordinate parameter naming diverges.** `vec_cu` takes `POS_X`/`POS_Y`,
  `mx_cluster_cu` takes `CU_X`/`CU_Y`, and the mesh generator special-cases each.
  One name.
- **`N_BUFFERS` is published in the capability register and nothing consumes it.**
  Its natural meaning — how many buffer ids a unit-to-unit transfer may address —
  is reasonable, and a receiver could range-check against it instead of against a
  local parameter. Today each unit checks its own, so the published number and
  the enforced one are different facts.
- **The forced conventions in §4 and §6 are prose, not checks.** A bindable
  simulation checker shipped with the framework — one that watches a unit's port
  and flags a withdrawn flit, a held unknown type, a double retirement — would
  turn most of this page into a test.
