# The simulators

Three, one per IR level, each answering a different question. `src/ktpu/interp/`.

```
   LEVEL 1  graph      ->  reference sim    what SHOULD the numbers be
   LEVEL 2  schedule   ->  engine sim       what numbers does the MACHINE get
   LEVEL 3  target     ->  mesh sim         how long does it take
                           xsim             is the RTL right
```

**A simulator depends only on its own level.** The mesh sim reads level-3, which
is nearly machine code. The engine sim reads level-2 and knows nothing about
flits. The reference sim reads level-1 and knows nothing about the machine at
all. So a kernel is checkable before a schedule exists, and a schedule is
comparable before there is any RTL to run it on.

---

## 1. Reference sim — level 1

Executes a `Graph` in **float64 numpy**. No dtypes below the graph's own, no
tiles, no engines, no cost.

Its only job is to be obviously right. It is what "correct" means for everything
below, so it is written the slow readable way and never optimised.

```python
out = reference.run(graph, {"x": arr, "g": gamma})
```

Uses: is the kernel the maths I meant; does a DSL rewrite change the answer; is
a fusion pass semantics-preserving.

---

## 2. Engine sim — level 2

Executes a `Schedule`, and this is the one that models **precision**.

Each engine applies its real arithmetic instead of float64:

| engine | operands | multiply | accumulate | out |
|---|---|---|---|---|
| matmul | quantised to `int7` + `E5M3` per 32 | exact integer | `ACC24` | `FP16` (saturating) |
| vector | converted to `E8M15` | `E8M15` FMA | `E8M15` | as stored |

That makes it the tool for the questions precision actually raises: does this
GEMM saturate at K=2048; does a split-K epilogue in `E8M15` recover what a
FP16 store loses; is `rsqrt`'s 0.55 ulp enough for this normalisation. All
answerable in seconds, none needing hardware.

It also walks the grid, so it checks what `Grid.covers` asserts: every output
element written exactly once, by the instance that should have written it.

**Cost, not cycles.** Per band it charges `max(compute, memory)` against the
`Target`, plus fixed per-round overhead. Good enough to rank two schedules and
to say which band is the limiter. Not a timing model and must not be read as one.

---

## 3. Mesh sim — level 3

`ktpu.interp.mesh.Mesh`. Reads a `Program` and nothing above it, so what it runs
is what the machine would be given: the same addresses, the same L1 spans, the
same loop counts, the same operand bindings.

```python
mesh = Mesh(prog, target)
mesh.upload(sched.bands[0], x, w)   # packs A and B tile-major, L1-entry order
mesh.bind("bias", b)                 # a named operand a VLD reads by name
mesh.run()
mesh.result()                        # the c region as a logical m x n array
```

**What it executes today: values.** `FILL`/`GEMM`/`DRAIN` against a
word-addressed image, `VLOOP` walking chunks, every `VALU` operand resolved to
the chain, a register, a scalar or a load. Formats round where the hardware
rounds — ACC24 and E8M15 to 16 significand bits, FP16 to 10 **and saturating at
65504**, with `mesh.saturated` counting how often. That counter is the point:
a silent clamp is the failure this machine is most able to hide, and it is what
`examples/04_run_it_and_check_it.py` demonstrates.

It **faults** rather than returning a plausible number — an address outside
every region, a `GEMM` whose L1 was never filled, an operand nothing is bound
to. A codegen bug should be a crash, not a wrong answer.

**Not yet modelled: time.** No X-then-Y routing, no link retry, no MAG port
contention, no per-round staging cost, no pipeline depth. Those are the
questions this simulator is *meant* to answer eventually — which port saturates,
what a round barrier costs, whether double buffering covers a fill — and none of
them are answered yet. Nothing in the current code reports a cycle count, so
there is no number here to mistake for one.

**Never bit-accurate.** It moves values, not bits; no DSP internal registers, no
barrel shifter LUT depth. That is xsim's job and there is no point duplicating
it.

---

## 4. Where xsim still rules

Everything bit-level: DSP configuration, pipeline alignment, the retry
handshake, Fmax. A **functional** disagreement between the engine sim and xsim
is a compiler bug and gets fixed in the compiler. A **timing** disagreement
means the mesh sim's constants are wrong and get re-measured.

The point is not to replace xsim but to make reaching for it rare — today every
question costs a full RTL run, including questions about arithmetic that has
nothing to do with the RTL.

---

## 5. Build order, and the argument against it

**Bottom-up would be the better order.** Level 3 is nearly machine code and the
machine already exists, so starting there gets something *running* soonest —
level 3 plus the mesh sim is a working toolchain against real hardware, while
levels 1 and 2 are a compiler with nothing to compile to.

We are not doing that, for one reason only: levels 1, 2 and the DSL were already
built when the question came up, and half-finishing them to start again lower
down wastes what exists. **Finish 1 and 2, then go straight to level 3** rather
than to the reference sim's refinements.

The order that follows from that:

1. **Reference sim.** Needs only level 1. Unblocks checking every DSL kernel.
   **Done** — `ktpu.interp.reference`.
2. **Level 3 + codegen.** The gap between a schedule and a running machine.
   **Done for a single-matmul graph**, values verified end to end against 1.
3. **Mesh sim**, which needs level 3. **Values done, timing not started** — §3.
4. **E8M15 / MXFP7 numeric models**, then the **engine sim** on top.
   `scripts/py/vec_tables.py` already has a bit-exact model of both Horner
   stages; extend to pack/unpack, the FMA and the four seeds.

Only 1 was out of dependency order, and it is there because it is a day's work
and makes every kernel written after it checkable.

**What 2 still needs**, and it is the same gap in both directions: a graph with
more than one matmul band has nowhere to put the second one's output. Every
matmul band drains to the one `acc`/`c` region, so two would overwrite each
other, and a value one band produces for a *later* band to read has no region at
all. Flash attention and MoE lower and codegen — they are exactly this shape —
and the mesh sim faults on them rather than running. See the handoff §6e.
