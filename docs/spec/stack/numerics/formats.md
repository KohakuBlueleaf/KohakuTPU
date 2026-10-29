# NUM: number formats

The number format at every stage of the card, the conversions between them,
the layouts that carry them, and the tolerances the stack checks values
against.

```
  DRAM            FP16 (IEEE binary16), FP32 (IEEE binary32), MXFP7 entries
    | mover quantiser (transform id 1)        | vector unpack (FP16 exact, FP32 RNE)
    v                                         v
  cluster L1      MXFP7: int7 + E5M3 / 32 K   vector lanes   E8M15
    | exact integer sum of one K block         | FMA, one rounding
    v                                         | pack: FP16 RNE (saturating), FP32 exact,
  accumulator     FP22 = S1 E7 M14, bias 63    |       MXFP7 (via FP16)
    | drain / emit: FP16 RNE, flush, saturate  v
    v                                        DRAM
  DRAM / core L1  FP16 (no subnormals, no inf)
```

Silicon evidence is the v9t6 card's 455 kernels (counts below from their
statement and instruction tally) and the LeNet run on v9t6 whose loss
matched CPU f32 (2.3320 / 2.2305 against 2.3324 / 2.2289). That run used: the mover quantiser (A and B
packings, `fn.quant` 172), cluster GEMMs with FP22 accumulation and FP16
drains (`fn.gemm` 4471, `fn.drain` 314), the mover GT4 transform
(`fn.gt4` 442), and the vector core's FP16 and GT4 unpack and FP16 pack
(`image:vunpk:fmt` f16 8131, gt4 450; `image:vpack:fmt` f16 5868). It did
not use FP32 pack or unpack, the vector MXFP7 pack, the SRU, or values near
FP16's range limits.

## Parameters

| parameter | v9t6 | meaning | source |
|---|---|---|---|
| `mg.kblock` | 32 | K elements sharing one scale | `software/language/kohakutpu/language/numerics/mxfp7.py:48`; machine file |
| `mx7.sebits` | 5 | scale exponent bits | `mxfp7.py:43` |
| `mx7.smbits` | 3 | scale mantissa bits | `mxfp7.py:44` |
| `mx7.sbias` | 20 | scale exponent bias | `mxfp7.py:45`; `src/kohakutpu/transform/mx_quant.v:47` |
| `mx7.qmax` | 63 | largest significand magnitude (int7, no -64) | `mxfp7.py:51` |
| GEMM `anchor` | 40 | the stack's anchor, `2 * mx7.sbias` | `mxfp7.py:46` |
| `mg.acc_mantissa_bits` | 14 | accumulator mantissa bits (FP22) | `src/kohakutpu/matmul/mx_cluster_cu.v:83` |
| `vc.sig_bits` | 16 | E8M15 significand bits, implicit one included | `software/language/kohakutpu/language/numerics/e8m15.py:13` |
| `vc.lanes` | 16 | f16 statistic values a 32-byte word | `software/application/kohakutpu/application/kernels/vector.py:44` |

## DRAM formats

- **NUM.MEM.001** `silicon` Owner rule: every tensor on the card is float: `f16`, `f32`, `mx7a`, `mx7b`. Integers are allowed only on the system node and on SIMT units. (doc: `docs/projects/kohakutpu/tinygrad.md:126-127`; evidence: the v9t6 kernels' buffers are f16 and MXFP7 only (the card-run tally `fn:<fn>:fmt` f16 1552); LeNet's labels are one-hot float)
- **NUM.MEM.002** `silicon` An `f16` buffer holds IEEE binary16, little-endian, row-major. (code: `ktpu-l1/src/layout/buffer.rs`; evidence: every v9t6 kernel)
- **NUM.MEM.003** `RTL` An `f32` buffer holds IEEE binary32. The vector core's F32 converters read it to E8M15 by NUM.E8.004 and write E8M15 zero-extended (exact), so an `f32` written by a lane is an E8M15 value with its low 8 mantissa bits zero. The F32 walk that moves it is NUM.F16.008. (code: `e8m15.py:5-7`; RTL `src/kohakutpu/vector/vec_cvt.v:117-163`, instantiated by `src/kohakutpu/vector2/v2_core.v:834,985-986`; evidence: `tests/vector/vec_cvt_tb.v` (drives `vec_cvt_f32_to_e8` and `vec_cvt_e8_to_f32`, the modules the core instantiates); no f32 walk on silicon)
- **NUM.MEM.004** `RTL` The tinygrad device computes on the f16 form of an f32 buffer: on the card every float buffer is held as f16, and an operand of a product as MXFP7. (doc: `tinygrad.md:61-63,111-117`; evidence: `software/application/tests/ktpugrad/test_tinygrad.py::test_program_on_ktpu[f32_input]` and the MATMUL cases (`linear_relu`, `k_major_weight`, ...) on the `dev` Verilated card)
- **NUM.MEM.005** `silicon` An `mx7a` / `mx7b` buffer holds MXFP7 entries (NUM.MX7.005), one byte an element counting the scale bytes; its layout keys are `gm, nk` (A) or `gn, nk` (B). (code: `software/language/kohakutpu/language/dsl/types.py:11,63`; evidence: `mm_` and `quant_` kernels, e.g. `mx7a[6400, 416] @dram(gm=64, nk=1)`)
- **NUM.MEM.006** `unproven` FP32 is not a matmul operand format: the quantiser reads FP16 only, so an f32 tensor reaches a cluster through its f16 form. (doc: `docs/projects/kohakutpu/number-format.md:308-322`; code: `mxfp7.py:104-116`)

## FP16 and the vector core's edges

- **NUM.F16.001** `silicon` FP16 to E8M15 (vector unpack) is exact: a normal value rebiases its exponent by +112 and zero-extends its mantissa by 5 bits; +-0 is +-0. (RTL `vec_cvt.v:16-44`, `v2_core.v:826-831`; doc: `docs/projects/kohakutpu/vector-core.md:57-62`; evidence: the card-run tally `image:vunpk:fmt` f16 8131, gt4 450)
- **NUM.F16.002** `RTL` An FP16 subnormal unpacks to the normal E8M15 of the same value; an FP16 exponent of 31 unpacks to E8M15 exponent 255 with the mantissa carried (inf stays inf, NaN stays NaN). (RTL `vec_cvt.v:24-42`; doc: `vector-core.md:116-125`; evidence: `tests/vector/vec_cvt_tb.v`, all 65,536 FP16 patterns and the FP16 round trip)
- **NUM.F16.003** `silicon` E8M15 to FP16 (vector pack) rounds the 16-bit significand to 11 bits to nearest even and rebiases by -112. (RTL `vec_cvt.v:77-112`; `src/kohakutpu/vector2/v2_cvt.v:28-76`, used by `v2_core.v:981-982`; evidence: the card-run tally `image:vpack:fmt` f16 5868)
- **NUM.F16.004** `unproven` A packed result below 2^-14 becomes an FP16 subnormal, rounded to nearest even (a carry gives the smallest normal); a result that rounds below the smallest subnormal is a signed zero; E8M15 zero packs to a signed zero. (RTL `v2_cvt.v:4-12,37-51,71`; `vec_cvt.v:91-111`; evidence: none; the V2 core packs through `v2_e8_to_f16` (`v2_core.v:981-982`), which no testbench drives; `tests/vector/vec_cvt_tb.v` drives `vec_cvt_e8_to_f16`, and `v2_cvt.v:1-2` states the bit identity in a comment (ISA.VC.107; Conflicts in `isa/vector.md`, item 11))
- **NUM.F16.005** `unproven` A finite packed result at or above 65520 saturates to +-65504 (0x7BFF / 0xFBFF); 65504 encodes exactly; an E8M15 infinity packs to an FP16 infinity (0x7C00); a NaN packs to a quiet NaN carrying mantissa bits [13:5]. (RTL `v2_cvt.v:69-75`; `vec_cvt.v:58-63,107-112`; evidence: none; as NUM.F16.004, `tests/vector/vec_cvt_tb.v:189-216` checks `vec_cvt_e8_to_f16`, not the core's `v2_e8_to_f16`)
- **NUM.F16.006** `silicon` A walk moves L1 words a chunk by mode: F16 (mode 0) one word a chunk, GT4 (mode 1) four words to four chunks. (code: `ktpu-isa/src/unit/vector/core.rs`; RTL `v2_core.v:174`; evidence: the card-run tally `image:vunpk:fmt`, `image:vpack:fmt`)
- **NUM.F16.007** `RTL` MX7 (mode 3, pack only) packs all eight chunks of a register into one entry (four words), chunk base 0 for the A packing. The faults are NUM.F16.009. (code: `ktpu-isa/src/unit/vector/core.rs`; RTL `v2_core.v:471,900,911`; evidence: `test_l1_examples.py::test_l2_mlp_quantises_its_activation_on_the_core`)
- **NUM.F16.008** `unproven` F32 (mode 2) moves two L1 words a chunk (eight lanes a word, the low word's lanes held for the high word's). (code: `ktpu-isa/src/unit/vector/core.rs`; RTL `v2_core.v:747-754,838-854`; evidence: no named test walks F32; the converters themselves are RTL, NUM.MEM.003)
- **NUM.F16.009** `unproven` An MX7 unpack faults F_MODE; so does an MX7 pack of other than 8 chunks, or with a chunk base other than 0 (A packing) or 1 (B packing), or on a core built without MX (`HAS_MX` 0). (RTL `v2_core.v:471,513-516`; evidence: none; no named test drives the faults)

## E8M15 lanes

- **NUM.E8.001** `silicon` A lane value is E8M15: 24 bits {sign, 8-bit exponent biased 127, 15-bit mantissa}; exponent 1..254 is `(-1)^s 2^(e-127) 1.m`, exponent 0 is zero whatever the mantissa (no subnormals), exponent 255 is infinity (m = 0) or NaN. The significand is `vc.sig_bits` [16] bits. (code: `e8m15.py:1-15`; doc: `vector-core.md:37-50`; evidence: every v9t6 vector kernel)
- **NUM.E8.002** `unproven` A lane FMA result rounds once to nearest even, except an effective subtraction whose discarded residue (alignment shift of 33 or more) sits next to the rounding boundary, which reads exactly one ulp high; a magnitude below 2^-126 flushes to a signed zero; at or above 2^128 it is infinite. (code: `e8m15.py:4-6,18-24`; RTL `src/kohakutpu/vector2/v2_alu.v`; doc: `vector-core.md:25-29,116-125,233-245`; evidence: none for the V2 core: `tests/vector/vec_alu_tb.v` (19 in 4,000 boundary-built cases one ulp high, 0 in 6,000 banded random) drives the V1 `vec_alu`, while the V2 lanes instantiate `v2_alu` (`v2_lanes.v:316`); Conflicts in `isa/vector.md`, item 11)
- **NUM.E8.003** `unproven` The four transcendental seeds (`vexp2`, `vlog2`, `vinv`, `vrsqrt`) are faithful, a few ulp from the correctly rounded value; they are not bit-equal to an exact reference. (doc: `vector-core.md:25-29`: 32-segment tables, a few ulp off; evidence: none for the V2 core: `tests/vector/vec_alu_tb.v` drives the V1 `vec_alu`; silicon ran `vinv` 664, `vexp2` 378, `vrsqrt` 131 statements, checked at kernel tolerance, which bounds no ulp count)
- **NUM.E8.004** `RTL` FP32 to E8M15 keeps the exponent, rounds the mantissa from 23 to 15 bits to nearest even, flushes an FP32 subnormal to a signed zero, and turns a carry out of the largest finite exponent into infinity; FP32 inf / NaN stay inf / NaN. E8M15 to FP32 is exact (exponent 0 gives a signed zero). (RTL `vec_cvt.v:117-163`; evidence: `tests/vector/vec_cvt_tb.v`)
- **NUM.E8.005** `silicon` A vector-core immediate (`seti`) written as a float is converted to E8M15 by the shared Rust RNE converter (RUL.056); 1.0 is 0x3F8000. An integer immediate is its low 24 bits. (code: `ktpu-isa/src/numerics.rs`; `ktpu-l1/src/lower/image/`; evidence: the card-run tally `image.seti` 320 statements)
- **NUM.E8.006** `unproven` An SRU constant (`KB`, `KC`, `MVAL`, `K0..K3`) is E8M15 rounded to nearest even from the float, a magnitude below 2^-126 a signed zero, 2^128 and up infinite, NaN 0x7FC000. (code: `ktpu-isa/src/numerics.rs`; `ktpu-l1/src/lower/sru.rs`; evidence: no silicon constant-conversion boundary witness; the SRU benches build constants with `scripts/py/sru/e8.py`; see NUM.X01)
- **NUM.E8.008** `silicon` A lane product of a zero is +0 whatever the signs: `-0 * 1` is +0, measured on the card. (RTL `src/kohakutpu/vector2/v2_alu.v`; evidence: vector `num/e8/NUM.E8.008.mul_zero_sign` on the v9t6 card and the Verilated v9 card `vlt_card_v9_1n_v2`, 2026-10-22)
- **NUM.E8.007** `RTL` A lane value with `|x|` < 2^14 rounds to the nearest whole number (ties to even) as `(x + 49152) - 49152` (49152 = 1.5 * 2^15); floor, ceil and truncation are built from it with compares. (code: `software/application/kohakutpu/application/kernels/expr.py:19-21,244-256`; evidence: `test_tinygrad.py::test_program_on_ktpu[floor_ceil]`)

## MXFP7

- **NUM.MX7.001** `silicon` An MXFP7 element is `q * scale`, q a signed 7-bit integer in [-`mx7.qmax`, `mx7.qmax`] [-63, 63] (no -64), scale `2^(E - mx7.sbias) * (1 + M/8)` from the 8-bit field `{E[4:0], M[2:0]}`; one scale covers one row (lane) over `mg.kblock` [32] consecutive K elements. (code: `mxfp7.py:1-51`; RTL `mx_quant.v:18-28,45-47`; doc: `number-format.md:17-29,82-104`; evidence: the card-run tally `fn.quant` 172; LeNet on v9t6)
- **NUM.MX7.002** `silicon` The quantiser reads FP16. A block's peak is the largest `bits[14:0]` of its 32 elements; a subnormal peak is renormalised. The scale is the smallest representable one with `peak / scale` ≤ 63 (rounded up): `m8 = ceil(sig_p / 126)`, `es = ep - 21`, or `ep - 20` with m8 folded to 8 or 9 when it reaches 16 or 17; `es` clamps to [-`mx7.sbias`, 31 - `mx7.sbias`] [-20, 11]; the stored field is `((es + 20) & 0x1F) << 3 | (m8 - 8)`. An all-zero block has es 0, m8 8 (field 0xA0) and q 0. (code: `mxfp7.py:62-101`; `ktpu-l1/src/layout/tensor.rs`; RTL `mx_quant.v:282-360`; doc: `number-format.md:119-133`; evidence: `fn.quant` on v9t6; `tests/sysnode/mx_quant_tb.v` against `mxfp7.py`)
- **NUM.MX7.003** `silicon` `q = round(x / scale)` through the reciprocal table `RECIP = round(4096 * 8 / m8)` {8: 4096, 9: 3641, 10: 3277, 11: 2979, 12: 2731, 13: 2520, 14: 2341, 15: 2185}: `prod = sig * RECIP[m8]`, `sh = 25 + 12 + es - e`, magnitude `(prod >> sh) + ((prod >> (sh - 1)) & 1)` (half rounds away from zero), 0 when `sh` > 40, clamped to 63 (a clamp finite FP16 inputs never reach: at the top `es` clamp 65504 / 3840 < 63; F-025), then signed. FP16 subnormal elements are decoded with exponent 1, not flushed. (code: `mxfp7.py:54-59,104-148`; RTL `mx_quant.v:109-129,388-427`; doc: `number-format.md:106-133`; evidence: `fn.quant`; `test_l1_examples.py::test_l2_mover_quantises_and_tiles` byte for byte)
- **NUM.MX7.004** `unproven` An FP16 infinity or NaN in a block has no defined MXFP7 value (its exponent field reads as the largest binade); the stack does not check operands for them. (code: `mxfp7.py:70-73,118-134` (no special case); RTL `mx_quant.v:128-129`)
- **NUM.MX7.005** `silicon` An entry is four 32-byte words for 4 lanes x 32 K: word w carries K [8w, 8w+8) as 32 int7 slots MSB-first from bit 255 (slot s at bits [255-7s : 249-7s]), slot `lane*8 + kk` in the A packing and `kk*4 + lane` in the B packing, and all four scale fields in bits [31:0], lane 0 highest ([31:24]); words are little-endian in memory; entries run tile-major (tile of `gm`/`gn` lane groups, K chunk of `nk` blocks, lane group, block). (code: `ktpu-l1/src/layout/tensor.rs`; doc: `number-format.md:137-163,241-244`; evidence: `fn.quant` (`bpack` for B); `test_l2_mover_quantises_and_tiles` byte for byte against `pack_a(x, 8, 2)`, `pack_b(x, 16, 4)`)
- **NUM.MX7.006** `RTL` The vector core's MXFP7 pack converts its lanes to FP16 by NUM.F16.003-005 (saturating at 65504) and quantises the FP16 by NUM.MX7.002-003; lane r of the entry is chunks 2r, 2r+1 (k = (chunk & 1) * 16 + lane). (code: `ktpu-isa/src/unit/vector/core.rs`; RTL `v2_core.v:1022-1034` (`v2_mxq` on `p_cv`, the converted FP16); evidence: `test_l1_examples.py::test_l2_mlp_quantises_its_activation_on_the_core` (value level, 2e-2))
- **NUM.MX7.007** `RTL` The SRU's MXFP7 out (OUT 2 / 3) packs its FP16 results the same way. (doc: `docs/projects/kohakutpu/sru.md:196`; evidence: `tests/sru/stream/sru_mx7_tb.v` against `mxfp7.py`, 6,952 words)
- **NUM.MX7.008** `RTL` An operand packed in K pairs (`nk` >= 2) quantises every zero (either sign) as 2^-24 (FP16 0x0001), whose q is 0 under every scale (`sh` >= 16, `prod` <= 4096): a block with a nonzero element keeps its field and elements, an all-zero block takes field 0x01 (E=0, M=1) in place of NUM.MX7.002's 0xA0. An `nk` = 1 packing quantises as NUM.MX7.002. The host packer (`tensor::pack`), vector-core `quant`, and fused vector `pack ... : mx7a/mx7b` stored into an `nk` >= 2 buffer or sent to a cluster apply it; mover QUANT cannot (NUM.MX7.002 in hardware), so it feeds no `nk` >= 2 contraction (RUL.099). (code: `ktpu-l1/src/layout/tensor.rs` (`ZERO_LIFT`, `lift`); `ktpu-l2/src/lower/mover/packing/image.rs`; `ktpu-l2/src/lower/vector/mod.rs` `mx7_pack`; `mxfp7.py` (`ZERO_LIFT`, `lift`); evidence: `test_zero_block_model.py`, `test_packing_model.py` byte for byte against `mxfp7.lift`; vector `num/mx7/NUM.MX7.008.zero_block_lifted`)

## Accumulator

- **NUM.ACC.001** `silicon` The accumulator float is S1 E7 M`mg.acc_mantissa_bits` [M14] = FP22: sign [21], exponent [20:14] biased 63, mantissa [13:0] with an implicit one; all-zero is zero. (RTL `src/kohakutpu/matmul/mx_fpacc.v:1-12`; `mx_acu_fp.v:23-24,54`; doc: `docs/projects/kohakutpu/accumulator.md:113-151`; evidence: every v9t6 GEMM)
- **NUM.ACC.002** `silicon` A K block's 32 products sum exactly as integers; the block's value is `part * (m8a * m8b) * 2^(ea + eb - anchor - 6)` with `m8 = 8 + M`, exact; it enters the accumulator float with one normalisation, and each later block is one float add. (RTL `mx_acu_fp.v:133-177`; doc: `number-format.md:45-60,167-190`; `accumulator.md:154-170`; evidence: every v9t6 GEMM; `tests/matmul/mx_cluster_tb.v` (exact integer datapath), `tests/matmul/mx_acu_fp_tb.v`)
- **NUM.ACC.003** `RTL` An accumulator add rounds to nearest even with the same effective-subtraction corner as NUM.E8.002 (one ulp high). (doc: `vector-core.md:242-243`; RTL `mx_fpacc.v:20` (`mx_fpacc_round_a/_b`); evidence: `tests/matmul/mx_acu_fp_tb.v`)
- **NUM.ACC.004** `silicon` A drained or emitted sub-tile converts FP22 to FP16: the mantissa rounds from 14 to 10 bits to nearest even (guard `m[3]`, sticky `m[2:0]`), the exponent rebiases by -48 (63 to 15), a rounding carry raises the exponent. (RTL `mx_fpacc.v:573-633`; evidence: every v9t6 drain, the card-run tally `fn.drain` 314)
- **NUM.ACC.005** `silicon` The drain conversion has no subnormals and no infinities: an accumulator zero, or a result with FP16 exponent ≤ 0 (below 2^-14), is a zero; a negative product that small drains as +0 (the drain keeps a sign, the GEMM path before it does not; measured on the card); a result with FP16 exponent ≥ 31 is +-65504 (0x7BFF / 0xFBFF), silently. A drained tile therefore never holds an FP16 subnormal or exponent 31. (RTL `mx_fpacc.v:606-627`; doc: `accumulator.md:354-385`; `number-format.md:323-326`; evidence: `tests/matmul/mx_fpacc_tb.v:274-292` ("saturate +", "saturate -", "flush to zero"); vector `num/acc/NUM.ACC.005.saturate_and_flush` on the v9t6 card and the Verilated v9 card `vlt_card_v9_1n_v2`, 2026-10-22)
- **NUM.ACC.006** `RTL` An add into another cluster's accumulator carries the FP22 value (two 256-bit granules a sub-tile), so a K split across clusters rounds to FP16 once, at the final drain. (RTL `mx_acu_fp.v:76-80,653`; doc: `accumulator.md:337-341`; evidence: `test_l1_examples.py::test_split_k_through_the_peer_accumulator`)
- **NUM.ACC.008** `RTL` With `nk` >= 2 the pump merges a K pair's two block partials before the accumulator: the partial with the smaller exponent sum `ea + eb` is shifted right by the difference (at most 30) and truncated, with no zero test. An all-zero block's partial (0, but exponent sum `E + e_partner`) thus truncates a live partner whose exponent sum is lower (NUM.MX7.002's E=20 against LeNet-ReLU b8 conv1 dW: error 10.2% of the output peak, 1.7% at E=0; causal P@V 256x64x256: mean error 4.8x). At E=0 (NUM.MX7.008) the zero partial's sum is its other operand's E alone, above the live sum only for live data below that operand's scale (conv1 dW: 5 pairs, shift 1). (RTL `mx_acu_fp_pump.v:339-382`; evidence: `scripts/py/measure/zeroblk/run.py` on `vlt_v9t6s_1n`; `test_zero_block_model.py`)
- **NUM.ACC.007** `unproven` Nothing in the stack detects or reports a drained value that saturated; the host readers (`ktpu_l1::layout::tensor::fp16_values`, `mxfp7.fp16_to_float`) read the drained FP16 with exponent 0 as zero and exponent 31 as an ordinary binade. (code: `ktpu-l1/src/layout/tensor.rs`; `mxfp7.py:175-182`; doc: `accumulator.md:384-385`)

## Layouts: sub-tiles, GT4, statistics

- **NUM.GT4.001** `silicon` A sub-tile word is a 4x4 FP16 block in one 32-byte word, element (i, j) at FP16 index `4i + j`. A drained tile buffer (`tile=(gm, gn)`) holds tiles row-major and sub-tiles row-major inside a tile. (code: `ktpu-l1/src/layout/tensor.rs`; `ktpu-l2/src/lower/memory.rs`; evidence: `mm_` kernels' `h1` buffers read by the cores)
- **NUM.GT4.002** `silicon` The GT4 transform (mover transform id 2, four words in, four out) treats each group of four 32-byte words as a 4x4 array of 64-bit granules and transposes it: out word i granule w = in word w granule i. On four row-major words (4 rows x 16 FP16) it yields the four sub-tiles of columns 4i..4i+3; it is its own inverse. (code: `ktpu-isa/src/unit/mover.rs`; `ktpu-l2/src/lower/mover.rs`; doc: `docs/spec/transform-slot.md:108-110`; evidence: the card-run tally `fn.gt4` 442)
- **NUM.GT4.003** `silicon` The vector core's GT4 walk applies the same transpose between four L1 words and four chunks; a GT4 register holds 4 rows x 32 columns, row i's columns 16q.. in chunk 4q + i. (code: `ktpu-isa/src/unit/vector/core.rs`; `ktpu-l2/src/lower/vector/`; RTL `v2_core.v:1041-1044`; evidence: the card-run tally `image:vunpk:fmt` gt4 450)
- **NUM.GT4.004** `unproven` A GT4 walk of a chunk count that is not a multiple of 4 is refused by the encoder and faults F_GT4 (10) in the core. (code: `ktpu-isa/src/unit/vector/core.rs`; RTL `v2_core.v:188,514,517`; evidence: no test drives the fault; `tests/vector2/v2_gt4_tb.v` covers the transpose only)
- **NUM.STAT.001** `RTL` A statistic (a reduction along rows) is R f16 values, `vc.lanes` [16] a 32-byte word; a kernel that reads or writes one moves 16 rows a trip (the row quantum is 16), its band registers merged lane by lane into one word and broadcast back. (code: `kernels/vector.py:44-56`; `kernels/matmul.py:37-39`; `software/language/kohakutpu/language/dsl/ops.py:431-454`; doc: `tinygrad.md:108`; evidence: `test_tinygrad.py::test_program_on_ktpu` [softmax, layernorm, max_rows, sum_rows] on `vlt_card_v9_1n_v2`; the v9t6 kernels used the earlier `[R, 32]` form, e.g. `rows_61adf15faf` out `f16[64, 32]`)
- **NUM.STAT.002** `RTL` A reduction's padding lanes hold its identity: 0 for a sum, -65504 (the lowest f16) for a max. (code: `software/application/kohakutpu/application/ktpugrad/run.py:1427`; doc: `tinygrad.md:108`; evidence: `test_tinygrad.py::test_program_on_ktpu[max_rows]`)

## SRU row-sum error budget

[RUL.098](../rulings.md#sru-accumulation-precision-lead-ruling-2026-10-24)
defines R2 precision. The following absolute bound is additional to upstream
function/FMA/input-conversion errors; it compares the sum of the actual finite
E8M15 inputs, S, to the accumulator value T before its final conversion.
Let W be the word count, N=4W the element count, E_j the running maximum
biased exponent after word j, and u_j=2^(E_j-150). Let n_j count the normal
finite elements with E_j-e>8 (only these can lose element bits). Let G be the
set of words after the first where E_j>E_(j-1).

Each element magnitude is replaced by its floor on the u_j grid and signed
afterward: its absolute error is less than u_j, zero if E_j-e<=8.
A growth step replaces the prior signed sum by floor(T/u_j)*u_j: its error
is nonpositive and greater than -u_j, including negative accumulators.
By induction, errors from previous words persist in value units; the triangle
inequality therefore gives

```
|T-S| <= B = sum_j n_j*u_j + sum_(j in G) u_j
          <= (N + W - 1) * u_final
```

The inequalities are strict when any nonzero local error occurs. A tighter
shape-only bound is `(N+2)*u_final`: growth exponents are distinct increasing
integers, so their quanta sum to less than `2*u_final` (geometric series).
All-zero rows have zero error. Signed cancellation makes a uniform relative
error bound impossible: budget this absolute error, not a fixed percentage.

These bounds assume no signed 48-bit accumulator wrap. Each element magnitude
is less than 2^24 integer units, and arithmetic rescaling cannot increase the
absolute integer magnitude beyond its previous value (except -1 persists).
Thus N<=2^23 is a sufficient no-wrap condition; use an explicit range proof
for larger rows. No finite rounding bound applies to NaN/infinity or wrap.

The final E8M15 result y has `|y-S| <= B + rho(T)`. For a finite normal
rounded result, `rho(T) <= 2^(floor(log2(abs(T)))-16)` (half a 16-bit-significand
ULP in T's binade, allowing carry into the next binade). For a flushed result,
use `rho(T) <= 2^-126`; for T=0 it is zero. Overflow to infinity has no finite
absolute bound. A conservative bound covering every finite final result is
`B + max(2^-126, abs(T)*2^-16)`. To compare against ideal RNE(S), add the
corresponding rho(S) as well. The compiler must include this budget per
reduction and propagate it through later operations; these are error bounds,
not permission to change bit-exact bench expectations.

## Lowered tinygrad reduction error budget

A tinygrad schedule AST is a logical kernel, not necessarily one arithmetic
format or one launch. Its initial `vector` plan classification does not rule
out MXFP7 execution. The following lowering rules describe software structure;
they do not upgrade the evidence labels on the numerical format rules above.

- `software/application/kohakutpu/application/ktpugrad/run.py:_vector` tries
  `window.tabled`, `_contraction`, and `window.contraction` before rows execution.
  `_window` materializes index windows and channel padding by relayout, then
  calls `_product`. `_product` converts A and B f16 forms to MXFP7. These
  conversions require NUM.MX7.002–003's absolute operand-quantization error even
  when the schedule's original plan kind is `vector` or its AST uses WHERE.
- `kernels/matmul.py:tile,generate` and
  `software/rust/crates/ktpu-kernels/src/gen/matmul.rs` partition output M/N
  tiles across clusters. Each tile accumulates successive K blocks in FP22,
  not an E8M15 reduction tree. K is padded to a multiple of 32; a launch can
  stream several blocks per K chunk. The cluster drain rounds to f16 before
  the E8M15 vector epilogue. Apply NUM.ACC.002–005 and include this intermediate
  f16 boundary, even if the epilogue is in the same runtime package.
- `runtime/tree.py:column` folds the top and bottom halves of R rows in E8M15,
  writes f16 partials, and repeats with `ceil(R/2)` rows. An odd final row is
  carried. There are `ceil(log2 R)` fold levels, followed by a rows epilogue.
  The first fold can evaluate the summand (for example, a product) before
  adding the pair. When read-view limits require it, the summand itself is
  materialized to f16 first. These are **f16 partial sums**, not a single
  full-precision reduction followed by one f16 store.
- `runtime/rows.py:run,_merge` emits `block_partial` f16 statistics for a row
  wider than one generated column block, then reduces those stored partials.
  The generated block width comes from the launch, not the AST reduction
  extent. `ktpu-l2/src/lower/vector/ctx/reduce.rs:reduce` folds column registers
  pairwise in E8M15 and applies its lane-reduction primitive; the alternative
  streaming path uses the SRU and its absolute truncation bound above.
- `ktpugrad/run.py:_materialise,_split_reduction` can split a multi-axis or
  nested reduction into multiple f16 temporaries. `_narrow` can materialize
  a non-reduction subexpression to f16 to respect the seven-input-view limit.
  Such boundaries must be represented in the error propagation, or the
  diagnostic must decline to certify that output.

For a finite, non-overflowing sum with ideal summands `t_i`, propagated
summand errors `e_i`, and at most d f16 storage boundaries on any summand's
path, storage error is bounded in **pre-cancellation absolute magnitude**:
use `A = sum(|t_i| + e_i)` and `gamma_d(u16) A`, not
`gamma_d(u16) |sum(t_i)|`, where `gamma_n(u)=n*u/(1-n*u)` and `u16=2^-11`.
A subnormal f16 store also contributes at most `2^-25` absolute error per
term/boundary, propagated through later operations. Arithmetic and storage
factors compose multiplicatively. Include the independently executed CPU
f32 reduction rounding (`u32=2^-24`) when using it as the comparison value.
Use an absolute bound for MXFP7 perturbations too:
`|delta(ab)| <= |a| e_b + |b| e_a + e_a e_b`.
The half-scale and reciprocal-table allowances use the magnitude of the
**actual converted operand**; a computed temporary cannot in general be
bounded by its source buffer's peak. Saturation, nonfinite input, or an
unrepresented intermediate prevents a finite rounding-only certificate.

`application/checks/path.py` observes the selected lowering, and
`application/checks/kernels.py` propagates these diagnostic budgets through
tinygrad CPU ASTs. Its depth bound counts all observed arithmetic launches
(including parallel branches conservatively) and both drain/epilogue stages
of a product package. It declines a finite bound for `_narrow` and for product
operands following computed temporary materialization, until their expressions
are propagated explicitly. A depth upper bound is not a claim about exact
serial stage count; the observed stage records carry that information.
The diagnostic policy is conditional on the cited format rules and is not a
new certified hardware accuracy guarantee. No observed output error is used
to choose or inflate a tolerance.

## Tolerances

`rel(got, want) = max|got - want| / max|want|` in float64 (want floored at
1e-9 in the runtime tests).

- **NUM.TOL.001** `RTL` Value checks use `rel`. (code: `software/application/tests/conftest.py:20-28`; `test_l1_examples.py:132-133`; evidence: every value check in `test_l1_examples.py`, `test_tinygrad.py`, `tests/api/test_tensor.py` and `tests/dsl/test_examples_card.py`)
- **NUM.TOL.002** `RTL` A cluster product against the MXFP7-quantised reference (`value_fp16(x) @ value_fp16(w).T` in float64): `rel` < 1e-2, also through split K, a drain into the node, data-dependent tiles and shared fills. (code: `test_l1_examples.py:127-178,314-323,397-400`; `mxfp7.py:163-172`; evidence: those tests on `vlt_card_v9_1n_v2`)
- **NUM.TOL.003** `RTL` A product whose activation is re-quantised between layers (on a core, or through layers with nothing in memory), against the quantised reference of the f16-rounded intermediates: `rel` < 2e-2. (code: `test_l1_examples.py:326-337,362-370`; evidence: test_l2_mlp_quantises_its_activation_on_the_core, test_l2_mlp_with_nothing_in_memory_between_layers)
- **NUM.TOL.004** `RTL` Vector-only paths against float64 numpy: silu 2e-3, add 1e-3, softmax 2e-3 (4e-3 on cluster scores), layernorm 4e-3, leaky ReLU 1e-3 (the `leaky` example, which the `select` pass turns into a max: no compare runs); a tile drained into the cores then silu, 2e-3 against silu of the f16-rounded quantised product. (code: `test_l1_examples.py:187-215,261-311,340-359`; `ktpu-l2/src/passes/select.rs`; evidence: those tests on `vlt_card_v9_1n_v2`)
- **NUM.TOL.005** `RTL` DSL examples on the card against their references (operands quantised by `_mx`): linear and splitk 2e-2; mlp, swiglu, attention, heads 3e-2. (code: `software/language/kohakutpu/language/examples/dsl/__init__.py:122-214`; `software/application/tests/dsl/test_examples_card.py:17`; evidence: `test_examples_card.py::test_example_on_the_card` on the `dev` Verilated card)
- **NUM.TOL.006** `RTL` Data movement, quantiser bytes and GT4 are bit-exact. (code: `test_l1_examples.py:52-123,373-394`; `test_tinygrad.py:255-264`; evidence: test_l1 mover tests and test_l2_mover_quantises_and_tiles; `test_tinygrad.py::test_program_on_ktpu` EXACT cases)
- **NUM.TOL.007** `RTL` tinygrad device programs other than products, against float64 numpy of the f16 inputs: `rel` < 2e-2. (code: `test_tinygrad.py:259-266`; doc: `tinygrad.md:121-127`; evidence: `test_tinygrad.py::test_program_on_ktpu` on the `dev` Verilated card)
- **NUM.TOL.008** `unproven` tinygrad products against float64 numpy of the unquantised f16 inputs: `rel` < 2e-2. Four cases miss on the Verilated card: k10 0.0239, n10 0.0216, n10_out, n84_epilogue 0.0221. (code: `test_tinygrad.py:186-212,259-266`; evidence: 76 of 80 cases pass on the current tree)
- **NUM.TOL.009** `unproven` Offline plans against the reference executor: `allclose` with rtol 3e-2 and atol 3e-2 times the reference's largest magnitude. (code: `software/application/tests/ktpugrad/test_compile.py:435-445`; evidence: none; `test_plans_compute` runs offline, not on a Verilated card or RTL bench)
- **NUM.TOL.010** `silicon` LeNet (`mnist_mlp.py --model lenet`, two steps of 64 and a 64-image eval) on v9t6 node 0 against CPU f32: loss 2.3320 / 2.2305 against 2.3324 / 2.2289, accuracy 0.5312 against 0.5469. This is the end-to-end witness, not a per-kernel bound. (evidence: the recorded 2026-10-21 v9t6 card result, `mnist_mlp.py --model lenet --no-jit`, no JIT)

## Rulings

The disagreements found on this page are ruled in `../rulings.md`: RUL.019, RUL.056, RUL.057, RUL.058, RUL.059, RUL.060, RUL.061, RUL.062.
