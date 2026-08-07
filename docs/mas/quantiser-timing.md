# The quantiser at 300 MHz

`src/kohakumas/mx_quant.v`. What the module does and why it is packed the way
it is; the format itself is [`../isa/memory.md`](../isa/memory.md) §6.

Out-of-context, `xcvu13p-fhgb2104-2L-e`, 300 MHz target (3.333 ns).

## 1. The starting point

The first version did the whole entry in one cycle: a 32-deep max reduction, an
11-step renormalise, an 11-bit divide and **128 parallel multiply-shift-round-
clamps**. It had never been synthesised — `src/kohakumas/` only became a
synthesis target when the memory port did — and it measured **32.5 MHz**, nine
times over budget.

Splitting it into five stages and balancing the max into a tree got it to
**159.7 MHz at 19,491 LUT and 128 DSP**. Two stages still missed:

| stage | delay |
|---|---|
| `PK_PEAK` 32→1 max, five compare levels | 5.311 ns |
| `PK_PACK` 128 × quant | 6.260 ns |

19,491 LUT over 128 elements is ~153 LUT each, and the multiply contributes
**zero** — it is a DSP48E2. **The area and the depth were 128 barrel shifters**,
each a 24-bit variable right shift over 0..40 positions.

## 2. What the module is actually asked to do

One invocation is one L1 entry: **8 AXI beats in, 4 operand words out**. On the
online read path (`mag_mem_port`'s read engine, used whenever an operand is not
pre-quantised) the fetch cannot deliver an entry faster than its eight beats.
So the budget is eight cycles of work per entry, not one — and a circuit sized
for one cycle is 8× wider than the demand.

That single observation is what pays for everything below.

## 3. The five changes

### 3.1 The peak rides on the fill beats

Beat `b` carries 16 elements of **exactly one lane** — lane `b/2`. So the block
maximum does not need a stage of its own: it needs two compare levels on cycles
already spent waiting for AXI.

```
   beat cycle    16 -> 4      two compare levels        -> r4[0..3]
   next cycle    fold r4 into acc[lane][0..1]           two levels
   PK_NORM       max(acc[0], acc[1]) is absorbed        (see 3.5)
```

The accumulator is **two wide per lane**, not one. One-wide would need
`max(acc, r4[0..3])` — three levels — in the fold cycle, and three levels of
15-bit compare-select is 3.2 ns against a 3.333 ns budget. Two-wide keeps every
cycle at two levels.

`PK_PEAK` does not become cheaper. It **disappears**.

### 3.2 `ceil(n_sig/126)` is eight compares, not a divide

`n_sig` is the renormalised peak significand, so it is in `[1024, 2047]` whenever
it is nonzero and the quotient is in `[9, 17]`. Its seven interior boundaries are
constants (`1134, 1260, … 2016`), so the divide is a compare ladder. That
removed the `PK_DIV` stage entirely — it had measured 3.030 ns on its own.

### 3.3 The pack is 32 elements wide, run four times

Output word `w` holds K-slice `[8w, 8w+8)` for all four lanes: exactly 32
elements. So one pass per output word, four passes, and the source select is a
plain 4:1 mux over the 128-word register file. Four passes plus the scale stages
still fit inside the eight beats the next fetch needs.

**4× off the LUTs and 4× off the DSPs**, for three cycles of latency.

### 3.4 The extraction is an 8-bit window, not a 24-bit barrel shift

This is the one that matters. Per element:

```
   prod = sig * recip(m8)            11 x 13, on a DSP48E2
   sh   = 37 + sexp - e
   mag  = min(63, (prod >> sh) + bit(prod, sh-1))
```

Two facts collapse it:

- **`prod < 2^23`.** `sig <= 2047`, `recip <= 4096`, so `prod <= 8,384,512`.
- **`sh >= 16`, always.** The block scale is derived from the block peak, and no
  element exceeds the peak. For a normal peak, `sexp = n_ep - 20` or `n_ep - 21`
  with `n_ep = e_peak`, so `sh = 37 + sexp - e >= 16 + (e_peak - e) >= 16`. For a
  subnormal peak every element is subnormal (`e = 1`) and `sexp` clamps to
  `SEXP_MIN = -20`, giving `sh = 16` exactly. Neither clamp can push it lower at
  `SBIAS <= 20`.

So with `t = sh - 16`:

- `t > 7` — the window sits entirely above the product; the result is **0**.
- `t in [0,7]` — `ext = prod[22:15] >> t`, and `mag = ext[7:1] + ext[0]`.
- `t < 0` — unreachable; the element would exceed the peak. Saturating to 63 is
  the safe direction and matches the "clamp, never wrap" rule the format needs.

A 24-bit shift over 41 positions becomes an **8-bit shift over 8 positions**.
Two LUT levels instead of thirteen, and roughly a fifth of the area.

### 3.5 Both accumulator halves are renormalised, and the winner selected after

`PK_NORM` first read `max(acc[0], acc[1])` and renormalised the result: a 15-bit
compare **in front of** an 11-step shift chain, 3.129 ns, and the binding stage
of the whole module at 317.7 MHz.

Renormalising both halves and selecting afterwards puts the compare **beside**
the chain and adds only a 2:1 mux to it — 2.182 ns. It is bit-exact because
renormalisation is a function of the value, so the normalised form of the larger
input is the normalised form of the maximum.

It costs four extra 11-bit normalisers, about 600 LUT. That is 14% of the module
for 83 MHz, on the module that was setting the design's clock.

### 3.6 The product register is the DSP's own

`pmul[s] <= sig * recip` infers `PREG=1` inside the DSP48E2 — a pipeline stage
at **zero fabric cost**, splitting the pack into

```
   pack stage 1   source select -> decode -> multiply -> PREG      1.937 ns
   pack stage 2   window -> round -> clamp -> sign -> word         1.647 ns
```

Vivado also absorbed the per-lane reciprocal register into the DSP's `AREG`.

## 4. Measured

```
   scripts/synth_check.tcl equivalent, per-stage query per endpoint bank --
   necessary because one dominant stage otherwise owns all 40 worst paths and
   the others never appear.
```

| | one cycle | 5 stages + tree | **this** |
|---|---|---|---|
| Fmax | 32.5 MHz | 159.7 MHz | **400.6 MHz** |
| WNS @ 3.333 ns | −27.435 ns | −2.927 ns | **+0.837 ns** |
| CLB LUT | 19,544 | 19,491 | **4,267** |
| CLB registers | 3,269 | 3,284 | **3,625** |
| DSP | 128 | 128 | **32** |
| BRAM / URAM | 0 / 0 | 0 / 0 | **0 / 0** |

Per stage, at the 3.333 ns target:

| stage | delay | slack |
|---|---|---|
| fill: 16→4 beat maximum | 1.954 ns | 1.389 |
| fill: fold into `acc` | 2.155 ns | 1.160 |
| `PK_NORM` renormalise + select | 2.182 ns | 1.133 |
| `PK_SCALE` ceil, scale, clamp, field | 1.748 ns | 1.567 |
| pack 1: select, decode, multiply → DSP | 1.937 ns | 1.079 |
| pack 1: shift control flags | 1.693 ns | 1.622 |
| pack 2: window, round, clamp, place | 1.647 ns | 1.636 |
| `src` write | 0.521 ns | 2.794 |
| control | 0.783 ns | 2.532 |

The worst path in the module is pack stage 1 into the DSP's `PREG`, 2.496 ns.

**In context**, one whole memory port — `mag_mem_port` with its intake queues,
read engine, write slots, emitter and AXI master around this quantiser:

| `mag_mem_port` | before | after |
|---|---|---|
| Fmax | 32.1 MHz | **330.0 MHz — meets** |
| WNS @ 3.333 ns | −27.793 ns | **+0.303 ns** |
| CLB LUT | 28,477 | **7,592** |
| CLB registers | 7,159 | **7,650** |
| DSP | 128 | **32** |
| BRAM / URAM | 0 / 0 | **0 / 0** |
| worst path | `u_quant/src_reg → u_quant/word0_reg` | `u_wrq` FIFO → `ws_rdy_reg` |

The quantiser is no longer anywhere near the binding path: what limits the port
now is the write-request FIFO's output into the slot-ready flags.

**URAM stays at 0** — it is reserved for a future FP16 vector unit. The 128-word
source buffer is flip-flops and says so (`ram_style = "registers"`): a fill beat
writes 16 words at once and the pack reads 32 through a 4:1 select, so no RAM
primitive can hold it.

## 5. Latency, and what it costs

`done` moves from **6 to 9 cycles** after the last beat.

```
   t     last beat            t+4..t+7  pack, one word per cycle
   t+1   fold into acc        t+8       last word lands, done asserted
   t+2   PK_NORM              t+9       done high, word0..3 stable
   t+3   PK_SCALE
```

The interface is unchanged: `start` / `b_layout` / `beat` / `beat_valid` /
`need_beat` / `done` / `word0..3`, `done` still a one-cycle pulse with the four
words stable when it fires. Both call sites — `mag_mem_port`'s read engine and
`mag`'s upload FSM — wait for `done` before touching the words, so extra latency
is admissible; neither has a fixed-cycle expectation.

It is not free, though. `mag_mem_port`'s read engine holds `m_rready` low in
`RS_WAIT`, so an entry costs eight fill cycles plus the quantiser's latency:
**16 → 19 cycles per entry**, a 19% throughput cost on the online read path.
That path is not the binding one — operands that are read repeatedly are
pre-quantised on upload and never touch this circuit — and 83 MHz of clock on
the module that gates the design is worth more than 3 cycles here.

*Reclaiming them* would mean folding the `acc` fold into the beat cycle (three
compare levels, ~3.2 ns) and issuing the first pack pass inside `PK_SCALE`
(putting the scale ladder in front of the DSP). Both are depth for latency, and
depth is the thing this module did not have.
