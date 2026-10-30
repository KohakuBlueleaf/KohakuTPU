# ISA: the Streaming Row Unit

The Streaming Row Unit (unit kind `SR`, RTL top `sru_cu` around `sru_core`)
as the stack drives it: the profile it reads, the configuration map and the
five instructions, the pool schedule, and the `send sr[..]` block the
compiler turns into them (`ktpu-l1::lower::sru`, `ktpu-isa::unit::sru`,
`ktpu-verify::sim::row`).

**No clause on this page is `silicon`.** The v9t6 card has no SRU: its die
is two clusters and two vector cores
(`src/kohakutpu/top/generated/ktpu_ship_1x2_2c2v_1m_nol2_pump.v`: MG
(1,1), (1,2); VC (2,1), (2,2); board `multimesh_v9t6` is four of these
dies). The only image with
SRUs is the `4+2s` die of board `sru_v9t7`, which is simulation only (no
bitstream). RTL evidence is the xsim benches of `scripts/py/xsim.py:2302-2367`
(`docs/projects/kohakutpu/sru.md:187-200`); no Verilated-card test runs a
compiled `send sr[..]` block.

## Parameters

| parameter | v9t6 | sru_v9t7 | meaning | source |
|---|---|---|---|---|
| `sr.count` | 0 | 2, at (2,1), (2,2) | SRUs a die (in the vector cores' places) | `ktpu-machine::card` |
| `sr.profile.F_LANES` | — | 16 | pool lanes: 16, 8, 4, or 0 (no pool) | `src/kohakutpu/sru/sru_core.v:41`; `sru_cu.v:50` |
| `sr.profile.H_TAPS` | — | 2 | earlier results one op may read: 1 or 2 | `sru_core.v:44`; `sru_cu.v:53` |
| `sr.profile.HAS_FN` | — | 1 | function front ends on the lanes | `sru_cu.v:51` |
| `sr.profile.TAB` | — | `ram` | function table: `ram` (1,024 x 36, loadable) or `rom` (exp2 only) | `sru_cu.v:52`; `sru.md:76-79` |
| `sr.profile.HAS_MANT` | — | 1 | MANT / HALF function modes | `sru_cu.v:62`; board `sru` |
| `sr.profile.FIN_RCP` | — | 1 | finalise RCP / RSQRT (lane 0's mantissa ROM) | `sru_cu.v:63`; board `sru` |
| `sr.profile.F16_IEEE` | — | 0 | FP16 form: 1 IEEE, 0 the clusters' accumulator form | `sru_cu.v:64`; board `sru` |
| `sr.buf_words` | — | 512 | sub-row buffer words (two halves) | `sru_cu.v:60` |
| `sr.fifo_depth` | — | 64 | A and B input FIFO words | `sru_cu.v:61` |
| `sr.burst` | — | 8 | words of an output burst, `min(room, 8)` | `sru_cu.v:271` |
| `sr.held` | — | 2 | sub-rows held past the output that has left: the buffer's halves | `sru_core.v:146` |
| `unit.inst_depth` | — | 512 | instruction FIFO words | `src/kohakutpu/sru/top/generated/ktpu_ship_1x2_4c2s_1m_nol2_pump.v:77,536` (`sru_cu.v:46` default 32) |
| `sr.npass` | — | 4 | passes a job | `ktpu-isa::unit::sru::config` |
| `sr.op_depth` | — | 16 | op store entries | `ktpu-isa::unit::sru::config` |
| `sr.fin_depth` | — | 16 | finalise program entries | `ktpu-isa::unit::sru::config` |
| `sr.rows` | — | 512 | row-state rows (three scalars each) | `sru_core.v:19`; `sru.md:123` |
| `sr.ldep` | — | 18 | slot cycles from an op's slot to its result | `ktpu-isa::unit::sru::config` |
| `sr.wlead` | — | 2 | slot cycles from a word's request to its first op | `ktpu-isa::unit::sru::config` |
| `sr.taps` | — | 32 | result-tap and word-slot depth | `ktpu-isa::unit::sru::config` |

## Images and profile

- **ISA.SR.001** `unproven` A program sends to `sr[J]` only on a machine with `sr.count` > J. v9t6 has `sr.count` 0; the card enumerates `SR` as an empty list. (code: `ktpu-l1::lower::target`; `software/application/tests/runtime/test_card_units.py:22`; evidence: none on a card with SRUs)
- **ISA.SR.002** `unproven` The image with SRUs is the `4+2s` die: SRUs at the vector cores' ports (2,1), (2,2), four clusters, no vector core; board `sru_v9t7` (design `multimesh_v9t7`, bitstream empty, simulation only); the Verilator harnesses `tests/sru/system/card_sru_1n.v`, `card_sru_2n.v` are generated from it. (code: `ktpu-machine::card`; `scripts/py/xsim.py:2361-2367`; evidence: the harnesses exist; no test runs on them)
- **ISA.SR.003** `unproven` The compiler reads two profile knobs, `sr.profile.F_LANES` and `sr.profile.H_TAPS`, from the machine (`params["SR"]`, from the board's `sru`), defaulting to F_LANES 16, H_TAPS 2. (code: `ktpu-l1::lower::sru`; `ktpu-machine::card`; evidence: none; `tests/sru/sru_core_tb.v` runs jobs scheduled by `_rs.sru_words` from `scripts/py/sru/core_vectors.py:25,68`, which takes the profile from `scripts/py/sru/profiles.py`, not from `ktpu-l1::lower::sru` or the machine)
- **ISA.SR.004** `RTL` The profiles are `strong` (16 lanes, RAM table with MANT/HALF, 2 taps), `rom` (16 lanes, exp2 ROM, 2 taps), `mid` (8 lanes, RAM table, 1 tap), `spu` (16 FMA lanes, 2 taps), `pack` (no pool, MXFP7 only); every profile has `F16_IEEE` 0. `sru_v9t7` carries `strong`. (code: `scripts/py/sru/profiles.py:8-29`; doc: `sru.md:173-185`; evidence: `tests/sru/sru_core_tb.v` per profile)

## Instructions

Each is one 256-bit payload, opcode in [255:252].

- **ISA.SR.010** `RTL` CFG (op 1): `word` [251:244] (an 8-bit configuration address), `value` [31:0]; writes one 32-bit configuration word. (code: `ktpu-isa::unit::sru`; RTL `src/kohakutpu/sru/sru_cu.v:6`; evidence: `tests/sru/sru_cu_tb.v`, encoded independently by `scripts/py/sru/cu_vectors.py:32-33`)
- **ISA.SR.011** `RTL` LOAD (op 2): `what` [251:250] (0 function table, 1 column constants, 2 row state), `first` [249:240], `addr` [239:200], `words` [199:188] 32-byte words. A table word holds 4 entries (entry k at bits `64k +: 36`), a column-constant word 2 (`128k +: 128`), a row-state word one row. (code: `ktpu-isa::unit::sru`; RTL `sru_cu.v:7-8,23-24`; evidence: `tests/sru/sru_cu_tb.v` (all three targets), `cu_vectors.py:36-43,108-109,133`)
- **ISA.SR.012** `RTL` RUN (op 3): `amem` [251] (A from memory 1, a COUNT stream 0), `peer` [250] (out to a unit 1, memory 0), `aaddr` [249:210], `astride` [209:194] words, `oaddr` [193:154], `ostride` [153:138], `baddr` [137:98], `bstride` [97:82], `px` [81:78], `py` [77:74], `pbuf` [73:70], `poff` [69:54] granules, `count` [53], `ax` [52:49], `ay` [48:45] (A's producer), `words` [44:21] (the stream's words, NW x NSR). (code: `ktpu-isa::unit::sru`; RTL `sru_cu.v:16-21`; evidence: `tests/sru/sru_cu_tb.v`, `cu_vectors.py:50-60,135,160,188,206`)
- **ISA.SR.013** `RTL` STORE (op 4): `first` [248:240], `addr` [239:200], `rows` [199:188]; one 32-byte word a row, R0, R1, R2 as f32 in [31:0], [63:32], [95:64]. (code: `ktpu-isa::unit::sru`; RTL `sru_cu.v:11-13`; evidence: `tests/sru/sru_cu_tb.v`, `cu_vectors.py:46-47,189`)
- **ISA.SR.014** `unproven` POST (op 5): `x` [251:248], `y` [247:244], `n` [15:0]; sends a CREDIT of `n` granules to (x, y). (code: `ktpu-isa::unit::sru`; RTL `sru_cu.v:14`; evidence: no bench vector issues it)
- **ISA.SR.015** `RTL` The SRU executes its words in order; CFG, LOAD, STORE and POST retire when done; RUN holds the unit until the job is done and its last write has its ack. (code: `ktpu-verify::sim::row`; RTL `sru_cu.v:9-10`; doc: `sru.md:155-161`; evidence: `tests/sru/sru_cu_tb.v`)

## Configuration map

The map is one table in `ktpu-isa::unit::sru::config`; `scripts/py/sru/cfg.py` generates the
RTL's `sru_cfg.vh` from it.

- **ISA.SR.020** `RTL` Global fields (word, low bit, width): `NW` (0x00,0,12) words a sub-row; `NPASS` (0x00,12,2) passes - 1; `BUF` (0x00,14,1); `USE_B` (0x00,15,1); `OUT` (0x00,16,2) 0 none, 1 f16, 2 MXFP7 A, 3 MXFP7 B; `MASK` (0x00,18,1); `R1` (0x00,19,1); `NSR` (0x01,0,16) sub-rows; `ROW0` (0x01,16,9); `COL0` (0x02,0,9); `DHI` (0x02,16,16) signed; `CLIM` (0x03,0,16); `MVAL` (0x04,0,24) E8M15; `COLOFF` (0x05,0,16) signed; `ROWOFF` (0x05,16,16) signed; `K0`..`K3` (0x08..0x0B,0,24) E8M15. (code: `ktpu-isa::unit::sru::config`; RTL `src/kohakutpu/sru/sru_cfg.vh` generated by `scripts/py/sru/cfg.py`; evidence: `tests/sru/sru_core_tb.v`)
- **ISA.SR.021** `RTL` Pass p's fields at `0x10 + 2p` (p < `sr.npass` [4]): `NOPS` (0,0,4) ops of its program (0: the word passes through), `OPBASE` (0,4,4), `R2EN` (0,8,1), `EMIT` (0,9,1), `FINLEN` (0,10,4), `FINBASE` (0,14,4). (code: `ktpu-isa::unit::sru::config`; evidence: `tests/sru/sru_core_tb.v`)
- **ISA.SR.022** `RTL` Op i of the op store at `0x40 + 4i` (i < `sr.op_depth` [16]): word 0 `KIND` (0 FMA, 1 function), `A` (2 bits), `B`, `C` (3 bits each), `NA`, `NC`, `RB`, `RC` (row scalar R0..R2), `CB`, `CC` (column constant C0/C1), `FIX`, `CS` (2), `PRE` (6, signed), `HA`, `HB`, `HC` (result tap), `MANT`, `NEG`; word 1 `KB` (24, E8M15) and `KW` (5, schedule); word 2 `KC` (24); word 3 `D0`, `D1` (5 each, tap depths). Sources: A in {X 0, B 1, O 2 (the pair partner's X, element j^1), H 3}; B and C in {K 0, R 1, C 2, B 3, X 4, H 5}. (code: `ktpu-isa::unit::sru::config`; evidence: `tests/sru/sru_core_tb.v`)
- **ISA.SR.023** `RTL` Finalise op i at `0x80 + i` (i < `sr.fin_depth` [16]): `OP` [4:0], `DST` [6:5] (R0 0, R1 1, R2 2, T 3), `A` [10:7], `B` [14:11], `C` [18:15]. Opcodes mov 0, add 3, sub 4, mul 5, fma 6, max 8, min 9, rcp 10, rsqrt 11; sources R0 0, R1 1, R2 2, T 3, ST1 4 (R1's row max), ST2 5 (R2's row sum), K0..K3 6..9. (code: `ktpu-isa::unit::sru::config`; RTL `sru_core.v:19-24`; evidence: `tests/sru/sru_core_tb.v`)
- **ISA.SR.024** `unproven` A field value that does not fit its width is refused (`pack` raises ValueError). (code: `ktpu-isa::unit::sru::config::pack`; evidence: none; `tests/sru/sru_core_tb.v` jobs are packed through `pack` with values that fit, which never takes the refusal)

## The pool schedule

- **ISA.SR.030** `RTL` A pass program of `NOPS` ops runs on `sr.profile.F_LANES` lanes with `M = 16 / sr.f_lanes` chunks a word and `II = NOPS * M` cycles a word; op i of chunk e issues at slot `i*M + e` of each II window, on the word taken `KW_i` windows earlier. (code: `ktpu-isa::unit::sru::config::schedule`; doc: `sru.md:94-99`; evidence: `tests/sru/sru_core_tb.v`, jobs scheduled by `_rs.sru_words`)
- **ISA.SR.031** `RTL` A result is readable `sr.ldep` [18] slot cycles after its op's slot; a word's first op slot is at least `sr.wlead` [2] cycles after the word is asked for; an op reading an earlier op's result reads it through a tap at depth `sigma_i - sigma_k - LDEP` in [0, `sr.taps`). (code: `ktpu-isa::unit::sru::config::schedule`; evidence: `tests/sru/sru_core_tb.v`)
- **ISA.SR.032** `unproven` The schedule refuses a program where an op reads the result of itself or a later op, where an op reads more than `sr.profile.H_TAPS` distinct earlier results, where a tap depth falls outside [0, `sr.taps`), or where `KW` reaches `sr.taps`; the emitter reports "the pool cannot run this program". (code: `ktpu-isa::unit::sru::config::schedule`; `ktpu-l1::lower::sru`; doc: `sru.md:97-99`; evidence: none; `tests/sru/sru_core_tb.v` runs only programs the schedule accepts, and a refusal is a software path)
- **ISA.SR.033** `unproven` One pass is in the pool at a time; a pass of no ops passes a word a cycle. Throughput at 16 lanes: a 1-op program a word a cycle, an n-op program a word per n cycles; 8 lanes halve it, 4 quarter it. (doc: `sru.md:56-57,113-114`; RTL `sru_core.v:13-17`; evidence: none; `tests/sru/sru_core_tb.v` checks the out stream word by word and the row state, and measures no cycle count)

## Semantics

- **ISA.SR.040** `RTL` A job is `NSR` sub-rows of `NW` words; sub-row g is rows 4g..4g+3, word h its columns 4h..4h+3, a 4x4 FP16 sub-tile with element (r, c) at lane 4r + c: the order a cluster drains a tile. (doc: `sru.md:29-30`; RTL `sru_core.v:5`; evidence: `tests/sru/sru_core_tb.v`)
- **ISA.SR.041** `RTL` `BUF` 0: pass 0 only, from A (and B with `USE_B`). `BUF` 1: pass 0 fills the sub-row buffer (R1 taps it) and passes 1..`NPASS` replay it; the fill of sub-row g+1 overlaps the replays of g. (code: `ktpu-isa::unit::sru::config`; RTL `sru_core.v:8-11`; doc: `sru.md:116-121`; evidence: `tests/sru/sru_core_tb.v`)
- **ISA.SR.042** `RTL` The chain is unpack (FP16 to E8M15, exact) -> mask (`col - row > DHI` or `col >= CLIM` takes `MVAL`) -> pool -> R2 (word-ordered row sum of the program's result with 8 guard bits, element magnitude truncation and signed accumulator rescaling as specified by [RUL.098](../rulings.md#sru-accumulation-precision-lead-ruling-2026-10-24); not an ideal single-rounding sum) -> FP16 -> MXFP7 pack (OUT 2/3) -> out; R1 is the row max of the words as they arrive (FP16 order). (RTL `sru_core.v:13`; doc: `sru.md:43-55`; evidence: `tests/sru/sru_core_tb.v`, `tests/sru/stream/sru_red_tb.v`, `sru_unpack_tb.v`)
- **ISA.SR.043** `RTL` An FMA op computes `(+-a) b + (+-c)` in E8M15 with one rounding; b is never negated. Its zeros are IEEE round-to-nearest, not the vector lane's (NUM.E8.008, ISA.VC.058): a zero product carries the sign `sa ^ sb`; a zero product with a nonzero addend returns the addend exactly; a zero addend takes the product's sign, so the product is never flipped; a zero result of two zero terms is -0 only when both are negative (`(+0) 1 + (-0)` and `(-0) 1 + (+0)` are +0, `(-0) 1 + (-0)` is -0); an exact cancellation is +0; a nonzero result below 2^-126 is the signed zero of its own sign. (RTL `src/kohakutpu/sru/lane/sru_plane.v:145`, `:164`, `:168`, `:211`, `:218`, `:579-586`; evidence: `software/tests/spec/test_sru_oracles.py` against `scripts/py/sru/oracle.py`) A function op computes `(-1)^sg (c0 + c1 t) 2^(es + n)` from a table segment, x = a and k = b: EXP (FIX 0, MANT 0) `w = v - (floor(k) << 24)`, FIX (FIX 1) v in [-16, 16) in 1/32 segments, MANT (MANT 1, FIX 0) segment from x's mantissa (1/x with NEG), HALF (MANT 1, FIX 1) from (e mod 2, m) (rsqrt with NEG, sqrt without; negative x is NaN); `v = x * 2^PRE`; `CS` scales the slope. (code: `ktpu-isa::unit::sru::config`; `ktpu-l1::lower::sru`; doc: `sru.md:64-88`; evidence: `tests/sru/lane/sru_plane_tb.v` (120,000 FMA, 60,000 function cases))
- **ISA.SR.044** `RTL` The finalise program of a pass runs once that pass's statistics land and the pool is empty, over the sub-row's 4 rows: ADD/SUB/MUL/FMA and RCP/RSQRT on pool lane 0 (RCP/RSQRT need `sr.profile.FIN_RCP` 1), MOV/MAX/MIN on a compare-select; MUL keeps a zero product's sign; MAX/MIN return +0 for a zero. (doc: `sru.md:123-132`; RTL `sru_core.v:19-24`; evidence: `tests/sru/sru_core_tb.v`)
- **ISA.SR.045** `RTL` Row state is `sr.rows` [512] rows of three E8M15 scalars (R0..R2), loaded per sub-row into a working set (two, by sub-row parity) and stored back after it; `ROW0` is the job's first row. (RTL `sru_core.v:19-21`; doc: `sru.md:123-125`; evidence: `tests/sru/sru_core_tb.v`, `tests/sru/sru_cu_tb.v` (STORE))
- **ISA.SR.046** `RTL` OUT 1 writes FP16 sub-tiles, one word a word in; OUT 2 / 3 writes MXFP7 entries in the A / B packing, one word per two in (`NW/2` a sub-row). (code: `ktpu-l1::lower::sru`; `ktpu-verify::sim::row`; RTL `sru_core.v:13`; evidence: `tests/sru/stream/sru_mx7_tb.v` against `numerics/mxfp7.py` (6,952 words), `tests/sru/sru_cu_tb.v` (OUT 2 to a peer))
- **ISA.SR.047** `RTL` FP16 in and out follows `sr.profile.F16_IEEE`: 0 reads exponent 0 as a signed zero and 31 as an ordinary binade, and writes round-to-nearest-even with a signed zero below 2^-14 and +-65504 at 2^16 and above (inf and NaN included); 1 is IEEE (subnormals, inf, NaN). (RTL `sru_core.v:26-28`; `src/kohakutpu/sru/stream/sru_e8f16_acc.v:4`; doc: `sru.md:134-141`; evidence: `tests/sru/stream/sru_unpack_tb.v` (all 65,536 FP16, both forms), `sru_e8f16_tb.v` (1,740,432 values))
- **ISA.SR.048** `unproven` A job meets the core's contracts: `NW` ≥ 4 when R2 feeds a finalise; `NW` a multiple of 8 for MXFP7 out; `NW` ≤ `sr.buf_words`/2 when buffered; stream B only on unbuffered jobs. The emitter checks none of them. (RTL `sru_core.v:33-34`; code: `ktpu-l1::lower::sru` (no check); evidence: none)

## Flow control

- **ISA.SR.050** `RTL` RUN with `amem` 0 takes A from a CU_DATA COUNT stream into buffer 0 only: at RUN it posts the producer (`ax`, `ay`) min(`sr.fifo_depth`, `words`) credits, then min(8, the rest) each time 8 words leave the FIFO, so the posts sum to `words`. A RUN whose `words` is not NW x NSR is a verifier finding. (code: `ktpu-verify::sim::row`; RTL `sru_cu.v:26-32,542-545`; evidence: `tests/sru/sru_cu_tb.v`, `cu_vectors.py:188,206`)
- **ISA.SR.051** `RTL` RUN with `peer` 1 sends output bursts of at most 8 words, never across a sub-row, each once the SRU holds its length in credits, into (`px`, `py`) buffer `pbuf` from granule `poff`, flagged COUNT with `count`. (code: `ktpu-verify::sim::row`; RTL `sru_cu.v:30-32`; evidence: `tests/sru/sru_cu_tb.v`, `cu_vectors.py:188`)
- **ISA.SR.052** `RTL` Memory reads go out only for FIFO space already reserved, in runs of at most 32 words; input runs at most two sub-rows plus the A FIFO ahead of the output that has left. (RTL `sru_cu.v:26-28`; code: `ktpu-verify::sim::row`; evidence: `tests/sru/sru_cu_tb.v`)
- **ISA.SR.053** `RTL` At the end of a program every word landed has been taken and every credit received spent. (code: `ktpu-verify::sim::row`; evidence: `tests/sru/sru_cu_tb.v` (posts sum to the stream))

## The `send sr[..]` block

The compiler's text form (`docs/projects/kohakutpu/ir/ktpu.md` s2.4).
Nothing runs this form on a model, so every clause here is `unproven`.

- **ISA.SR.060** `unproven` A send block holds `job`, `pass` (a block of `op` and `fin`), `load`, `run`, `store`, `post`; none binds a name and only `pass` takes a block. (code: `ktpu-l1::lower::sru`)
- **ISA.SR.061** `unproven` `job` sets every global field, a field left out is 0; `buf`, `use_b`, `mask`, `r1` may be written as flags; `out=` takes `none | f16 | mx7a | mx7b`; `mval`, `k0`..`k3` are numbers converted by the shared `ktpu-isa::numerics::e8m15` conversion; a signed field takes a negative constant. A `job` clears the passes, ops and finalise program. (code: `ktpu-l1::lower::sru`)
- **ISA.SR.062** `unproven` `pass P [r2] [emit]` with P in 0..`sr.npass`-1, each once; its `op`s are laid into the op store and its `fin`s into the finalise program after the previous pass's; more than `sr.op_depth` ops or `sr.fin_depth` finalise ops is refused. A pass after 0 needs `job buf`; with `buf` and no `npass`, NPASS is the highest pass written. (code: `ktpu-l1::lower::sru`)
- **ISA.SR.063** `unproven` `op fma a=SRC b=SRC c=SRC` and `op fn a=SRC k=SRC [fix | mant [neg] | half [neg]] [cs=N] [pre=N]` (`pre` signed 6 bits). A source is X, B, O (a only), a number (an E8M15 constant), R0..R2, C0, C1, B, X (b, c), or H<k> (op k's result, k below this op); a and c may be negated, b may not. (code: `ktpu-l1::lower::sru`)
- **ISA.SR.064** `unproven` `fin OP DST <- SRC[, SRC...]` with the operand count of OP (mov 1, add/sub 2 as A, C; mul/max/min 2 as A, B; fma 3; rcp/rsqrt 1), DST in R0 R1 R2 T, sources as ISA.SR.023. (code: `ktpu-l1::lower::sru`)
- **ISA.SR.065** `unproven` `run` first emits the CFG words whose values differ from what this program last wrote to the unit (all of them the first time), then the RUN; `run a=ADDR|recv [from=UNIT] [astride=W] [b=ADDR] [bstride=W] out=ADDR|UNIT [ostride=W] [buf=B] [at=GRANULE] [count]`, `a=recv` needs `from=`; strides default to NW (A, B) and the out words a sub-row (NW for f16, NW/2 for MXFP7); `words` is NW x NSR when A is a stream, else 0. (code: `ktpu-l1::lower::sru`; `ktpu-isa::unit::sru`)
- **ISA.SR.066** `unproven` `load table|cols|rows <- ADDR n=WORDS [at=FIRST]`, `store rows -> ADDR n=ROWS [at=FIRST]`, `post n=CREDITS to=UNIT`. (code: `ktpu-l1::lower::sru`)

## Rulings

The disagreements found on this page are ruled in `../rulings.md`: RUL.012, RUL.013, RUL.014, RUL.054, RUL.056.
