---
title: NUM: number formats
summary: The number format at every stage of the card, the conversions between them, the layouts that carry them, and the tolerances the stack checks values against.
tags:
  - spec
  - stack
  - numerics
---
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
| `mg.kblock` | 32 | K elements sharing one scale | `software/kohakutpu/language/kohakutpu/language/numerics/mxfp7.py:50`; machine file |
| `mx7.sebits` | 5 | scale exponent bits | `mxfp7.py:45` |
| `mx7.smbits` | 3 | scale mantissa bits | `mxfp7.py:46` |
| `mx7.sbias` | 20 | scale exponent bias | `mxfp7.py:47`; `src/kohakutpu/transform/mx_quant.v:47` |
| `mx7.qmax` | 63 | largest significand magnitude (int7, no -64) | `mxfp7.py:53` |
| GEMM `anchor` | 40 | the stack's anchor, `2 * mx7.sbias` | `mxfp7.py:48` |
| `mg.acc_mantissa_bits` | 14 | accumulator mantissa bits (FP22) | `src/kohakutpu/matmul/mx_cluster_cu.v:83` |
| `vc.sig_bits` | 16 | E8M15 significand bits, implicit one included | `software/kohakutpu/language/kohakutpu/language/numerics/e8m15.py:18` |
| `vc.lanes` | 16 | f16 statistic values a 32-byte word | `software/kohakutpu/application/kohakutpu/application/kernels/classes/base.py:28` |

## DRAM formats

- **NUM.MEM.001** `unproven` Buffer specs accept f16, f32, mx7a, mx7b, u8, u32, i32 and u64. The buffer-type check does not restrict integer buffers to the sysnode or SIMT; operation and lowering checks determine operation legality. (code: software/kohakutpu/language/kohakutpu/language/dsl/types.py:7-12, :37-47)
- **NUM.MEM.002** `silicon` An f16 element is little-endian IEEE binary16. Without tiling, packing preserves row-major order after optional last-two-axis transpose. tile layout instead orders tiles and their literal 4 by 4 sub-tiles (matching v9t6 mg.lanes=4) row-major. (code: software/kohakutpu/compiler/crates/ktpu-l1/src/layout/buffer.rs:232-260, :460-495; evidence: Card f16 buffers; ordering matches af7c1bb:software/compiler/kohakutpu/compiler/layout/buffer.py:96-112, :145-149.)
- **NUM.MEM.003** `RTL` Memory F32 conversion interprets IEEE binary32. FP32-to-E8M15 follows NUM.E8.004. E8M15-to-FP32 appends eight zero mantissa bits; exponent-zero inputs become signed zero regardless of mantissa. Converter coverage does not establish the core's F32 memory walk. (code: src/kohakutpu/vector/vec_cvt.v:117-161; evidence: tests/vector/vec_cvt_tb.v:228-250 checks round trips and signed noncanonical zeros.)
- **NUM.MEM.004** `unproven` Runtime readers obtain an f16 form by reusing it, compacting padded rows, materialising a view form, converting retained host bytes and uploading, or casting valid f32 primary bytes on-device. Otherwise they raise FormError. The primary f32 allocation can coexist with its f16 form; not all stored bytes are f16. (code: software/kohakutpu/application/kohakutpu/application/runtime/forms.py:223-269; evidence: No qualifying witness established for the complete current path set.)
- **NUM.MEM.005** `unproven` mx7a requires gm/nk and mx7b gn/nk layout keys, excluding optional t and `node`; `node` requires @stage. Allocation rounds to whole MXFP7 entries: ceil(elements/(mg.lanes*mg.kblock))*entry_bytes [lanes=4, kblock=32, entry_bytes=128 on v9t6], with entry geometry from machine facts. (code: software/kohakutpu/compiler/crates/ktpu-l1/src/layout/buffer.rs:131-176, :191-210; evidence: af7c1bb:software/compiler/kohakutpu/compiler/layout/buffer.py:115-119 returns one byte per element without entry rounding.)
- **NUM.MEM.006** `unproven` An f32 tensor MXFP7 form is prepared from f16 storage: packing uses compatible held f16 rows or obtains its f16 form before packing. The mover quantiser consumes FP16 input beats; vector packing instead operates on lane-register values. (code: software/kohakutpu/application/kohakutpu/application/runtime/forms.py:223-269, :856-861, :920-935; src/kohakutpu/transform/mx_quant.v:53-57, :70-73; src/kohakutpu/vector2/v2_core.v:1058-1064)

## FP16 and the vector core's edges

- **NUM.F16.001** `silicon` FP16-to-E8M15 preserves every finite FP16 value exactly. Normal exponents are rebased by +112, the mantissa gains five zero bits, and signed zeros retain their sign. (code: src/kohakutpu/vector/vec_cvt.v:20-42; src/kohakutpu/vector2/v2_core.v:864-865; evidence: Card tally image:vunpk:fmt f16 8131 and gt4 450; tests/vector/vec_cvt_tb.v:156-169 additionally checks every FP16 encoding.)
- **NUM.F16.002** `RTL` FP16 subnormals normalize to E8M15 values of equal magnitude. FP16 exponent 31 becomes E8M15 exponent 255, with its mantissa followed by five zeros, preserving infinity versus NaN. (code: src/kohakutpu/vector/vec_cvt.v:24-42; evidence: tests/vector/vec_cvt_tb.v:156-187 checks all FP16 encodings and round trips.)
- **NUM.F16.003** `silicon` V2 FP16 packing rounds the significand from vc.sig_bits [16] to 11 bits using guard, sticky and retained-low-bit round-to-nearest-even. Bias changes from 127 to 15 and rounding carry advances the exponent. (code: src/kohakutpu/vector2/v2_cvt.v:32-51, :58-75; src/kohakutpu/vector2/v2_core.v:1014-1016, :1034-1038; evidence: Card tally image:vpack:fmt f16 5868.)
- **NUM.F16.004** `unproven` V2 FP16 packing shifts the significand into FP16 subnormal position and rounds once to nearest even. Carry can produce the smallest normal; sufficiently small magnitudes produce signed zero. E8M15 exponent-zero inputs become signed zero regardless of mantissa. (code: src/kohakutpu/vector2/v2_cvt.v:37-51, :68-75; evidence: No named V2 converter witness established.)
- **NUM.F16.005** `unproven` V2 FP16 packing saturates finite overflow to signed 65504; 65504 is exact and the magnitude-65520 tie also yields signed 65504. Infinity retains its sign; NaN becomes a quiet FP16 NaN retaining sign and E8M15 mantissa bits 13:5. (code: src/kohakutpu/vector2/v2_cvt.v:49-51, :68-75; evidence: No named V2 converter witness established.)
- **NUM.F16.006** `silicon` F16 walk mode 0 transfers one die.word_bytes [32]-byte L1 word per chunk. GT4 mode 1 transfers groups of mg.lanes [4] words/chunks, transposing a four-by-four array of die.word_bytes/4 [8]-byte granules. (code: src/kohakutpu/vector2/v2_core.v:174, :781-804, :851-865, :925-945, :1074-1080; src/kohakutpu/vector2/v2_gt4.v:66-94, :103-110; evidence: Card tally vunpk f16 8131/gt4 450 and vpack f16 5868.)
- **NUM.F16.007** `unproven` MX7 pack admission requires the full vc.chunks [8] chunks and chunk base zero or one; base one selects B packing. Packing reads all chunks twice using the read-counter chunk index rather than base, producing ceil(mg.entry_bits/(8*die.word_bytes)) [4] packed words. (code: src/kohakutpu/vector2/v2_core.v:469-471, :926-945, :1058-1064; evidence: No named RTL witness of this complete packing mode established.)
- **NUM.F16.008** `unproven` F32 walk mode 2 transfers two die.word_bytes [32]-byte L1 words per register chunk. Unpack converts die.word_bytes/4 [8] FP32 elements per word, retaining first-word lanes until second arrival. Pack reads the chunk for both words and selects lower then upper lanes for conversion. (code: src/kohakutpu/vector2/v2_core.v:781-804, :867-888, :925-945, :1018-1020, :1034-1038; evidence: Converter tests alone do not establish complete walk coverage.)
- **NUM.F16.009** `unproven` MX7 unpack faults F_MODE. MX7 pack faults F_MODE if HAS_MX is zero, encoded chunk count is not eight, or chunk base is neither zero nor one; the two bases select A/B packing. (code: src/kohakutpu/vector2/v2_core.v:469-471, :519-530; evidence: No named fault test established.)

## E8M15 lanes

- **NUM.E8.001** `unproven` Lane storage is E8M15: sign, eight-bit exponent biased 127 and vc.sig_bits-1 [15] fraction bits. Ordinary nonzero arithmetic uses an implicit leading one. Arithmetic zero flags classify exponent zero regardless of fraction; exponent 255 is infinity for zero fraction and NaN otherwise. Comparisons order raw sign/magnitude fields, with a special equality case for the two all-zero magnitudes; they do not canonicalize every exponent-zero payload first. (code: src/kohakutpu/vector2/v2_alu.v:134-163, :281-285, :295-318, :337-348, :737-787; evidence: Card vector tally establishes ordinary format use, not arbitrary noncanonical exponent-zero payloads.)
- **NUM.E8.002** `unproven` Lane FMA aligns addend in a 48-bit window, retains discarded alignment bits as sticky, forms signed sum, normalizes magnitude and rounds using guard/sticky/least-significant bit. Effective subtraction does not subtract discarded alignment residue. Exponent bounds apply after rounding carry: biased exponent <=0 produces signed zero, >=255 signed infinity. Exact cancellation of genuinely nonzero terms yields positive zero; NaN/infinity specials take precedence. (code: src/kohakutpu/vector2/v2_alu.v:332-348, :579-619, :648-701, :722-787; evidence: No current named V2 test establishing a universal one-ulp bound verified.)
- **NUM.E8.003** `unproven` Transcendental seeds use table-driven polynomial approximation with common normalizer/rounder. EXP2 indexes reduced fraction; LOG2/reciprocal index significand; reciprocal square root also uses exponent parity. Segment-origin identity cases substitute exact polynomial constants. Implementation alone establishes no uniform ulp bound. (code: src/kohakutpu/vector2/v2_alu.v:464-490, :522-548, :624-660, :687-743; evidence: No named V2 accuracy test establishing faithful rounding or fixed ulp bound verified.)
- **NUM.E8.004** `RTL` FP32-to-E8M15 rounds 23 mantissa bits to 15 with nearest-even rounding, advancing exponent for a carry. Exponent-zero inputs flush to signed zero; overflow becomes signed infinity. Infinity preserves sign; NaN becomes quiet E8M15 NaN retaining sign and FP32 mantissa bits 21:8. Reverse conversion appends eight zeros, except exponent-zero inputs become signed zero. (code: src/kohakutpu/vector/vec_cvt.v:121-161; evidence: tests/vector/vec_cvt_tb.v:228-250 checks round trips and exponent-zero conversion; :254-279 checks rounding, flushing, overflow, infinity and NaN.)
- **NUM.E8.005** `unproven` Floating seti literals use shared Rust E8M15 conversion: nearest ties-even, original magnitudes below 2^-126 to signed zero, exponent overflow to signed infinity, NaN to 0x7FC000. 1.0 encodes 0x3F8000; integer literals contribute low 24 bits. (code: software/kohakutpu/compiler/crates/ktpu-l1/src/lower/image/sync.rs:181-209; software/kohakutpu/compiler/crates/ktpu-isa/src/numerics.rs:6-54; evidence: af7c1bb:software/compiler/kohakutpu/compiler/encode/vector.py:294-297 converts to FP32 then drops eight low bits, rather than current direct ties-even conversion.)
- **NUM.E8.006** `unproven` The shared Rust E8M15 constant converter rounds finite significands to nearest even, flushes magnitudes below 2^-126 to signed zero, propagates rounding carry and emits signed infinity when the resulting exponent reaches 255. It preserves signed zero and infinity; NaN maps to 0x7FC000. (code: software/kohakutpu/compiler/crates/ktpu-isa/src/numerics.rs:6-54)
- **NUM.E8.008** `unproven` Multiplication uses a positive-zero addend. With a finite zero factor its result is positive zero rather than the factor-sign XOR. A zero factor forces both effective product exponents to one; NaN operands and zero times infinity take the exceptional NaN path. (code: src/kohakutpu/vector2/v2_alu.v:250-264, :325-348, :579-619, :630-638, :684-701, :777-784; evidence: The referenced num/e8/NUM.E8.008.mul_zero_sign card vector is not available in this snapshot.)
- **NUM.E8.009** `unproven` For canonical finite operands, a zero FMA product returns the addend, including its zero sign. Addition selects its second operand as the addend; subtraction selects its sign-negated second operand. Exponent-zero payloads are canonicalized to signed zero, and NaN operands or zero times infinity take the exceptional NaN path rather than an unconditional bitwise pass-through. (code: src/kohakutpu/vector2/v2_alu.v:250-268, :281-285, :325-348, :579-619, :630-638, :684-701, :768-787; evidence: The referenced reviews/zsign/probe3.log is not available in this snapshot.)
- **NUM.E8.007** `unproven` Nearest-integer pattern adds/subtracts 1.5*2^(vc.sig_bits-1) [49152] for magnitudes below 2^(vc.sig_bits-2) [16384]. Truncation uses signed offset magnitude 2^(vc.sig_bits-1) [32768], computes r=(x+offset)-offset, corrects r downward by r>x for nonnegative x or upward by r<x for negative x, and returns x unchanged outside that bound. (code: software/kohakutpu/compiler/crates/ktpu-kernels/src/expr/emit.rs:61-97; evidence: No named RTL witness of these patterns established.)

## MXFP7

- **NUM.MX7.001** `silicon` MXFP7 is q times scale, q in [-mx7.qmax,mx7.qmax] [-63,63], excluding -64. Scale E5M3 is 2^(E-mx7.sbias)*(1+M/8). One scale covers mg.kblock [32] consecutive K values in a lane: A row or B column. (code: software/kohakutpu/language/kohakutpu/language/numerics/mxfp7.py:37-53, :164-169; src/kohakutpu/transform/mx_quant.v:18-32, :47, :431-436; evidence: Card quant/LeNet format; constants match af7c1bb:software/language/kohakutpu/language/numerics/mxfp7.py:43-51.)
- **NUM.MX7.002** `silicon` The host quantizer selects the block's largest FP16 bits 14:0, normalizes a nonzero subnormal peak and computes m8=ceil(sig/126). It uses es=ep-21, except m8 of 16 or 17 becomes 8 or 9 with es=ep-20, then clamps es to [-20,11] without recomputing m8. Zero peak uses es=0, m8=8. Exponent clamping means this is not always the smallest representable scale bounding peak/63. (code: software/kohakutpu/compiler/crates/ktpu-l1/src/layout/tensor.rs:115-136, :175-176; software/kohakutpu/language/kohakutpu/language/numerics/mxfp7.py:72-103; src/kohakutpu/transform/mx_quant.v:288-368; evidence: Card fn.quant; algorithm matches af7c1bb:software/language/kohakutpu/language/numerics/mxfp7.py:70-100.)
- **NUM.MX7.003** `silicon` Host quantization uses RECIP={4096,3641,3277,2979,2731,2520,2341,2185} for m8=8..15. Subnormal significands omit the hidden one and use effective exponent 1. With prod=sig*RECIP and sh=37+es-e, sh>40 gives zero; otherwise the first discarded bit is added to the shifted magnitude, which is clamped to 63 and signed. This reciprocal approximation is not exact division followed by rounding. (code: software/kohakutpu/compiler/crates/ktpu-l1/src/layout/tensor.rs:75-77, :185-205; src/kohakutpu/transform/mx_quant.v:401-436; evidence: Card fn.quant; host arithmetic matches af7c1bb:software/language/kohakutpu/language/numerics/mxfp7.py:117-148.)
- **NUM.MX7.004** `unproven` Unchecked host quantization treats FP16 exponent 31 through ordinary peak-scale and significand calculations without rejecting infinity or NaN. Checked vector packing classifies inputs before VPACK and records nonfinite failure. These paths therefore differ in admission. (code: software/kohakutpu/compiler/crates/ktpu-l1/src/layout/tensor.rs:115-136, :185-201; software/kohakutpu/compiler/crates/ktpu-l2/src/lower/mover/packing/finish.rs:27-48, :55-101)
- **NUM.MX7.005** `silicon` An entry covers mg.lanes [4] by mg.kblock [32] values in ceil(mg.entry_bits/(8*die.word_bytes)) [4] words of die.word_bytes [32] bytes. On v9t6 word w carries K[8w,8w+8); int7 slot s occupies bits [255-7s:249-7s], with slot lane*8+kk for A and kk*4+lane for B. Each word repeats low-bit lane scales, lane zero highest [31:24]. Words are little-endian. Entries run tile, K chunk, lane group, block; tiles contain gm/gn groups, chunks nk blocks. (code: software/kohakutpu/compiler/crates/ktpu-l1/src/layout/tensor.rs:41-62, :208-214, :267-300; src/kohakutpu/transform/mx_quant.v:439-457; evidence: Card fn.quant including bpack witnesses layout.)
- **NUM.MX7.006** `unproven` Vector MXFP7 packing passes E8M15 lanes through the V2 FP16 converter and supplies the resulting FP16 words to v2_mxq with the selected A/B flag and chunk index. (code: src/kohakutpu/vector2/v2_core.v:1014-1016, :1034-1040, :1058-1064; evidence: No existing named RTL test established for the complete conversion path.)
- **NUM.MX7.007** `RTL` The SRU MX7 packer converts FP16 sub-tile streams to A or B MXFP7 word layout, clamping signed magnitudes to 63 and assembling scale fields in each output word. Its independent bench checks every output word against selected A/B vector files and requires expected count with no errors; this bench alone does not establish OUT decoding integration. (code: src/kohakutpu/sru/stream/sru_mx7.v:50-85, :310-352; evidence: tests/sru/stream/sru_mx7_tb.v:14-18, :38-65, :73-76, :98-122 checks packer A/B outputs.)
- **NUM.MX7.008** `unproven` Host packing with nk>=2 replaces either FP16 zero by 0x0001 before quantization; nk<2 leaves it unchanged. An all-zero block then has es=-20, m8=9, field 0x01 and zero quantized significands. Vector packing similarly replaces zero lanes before classification and VPACK when its lift flag is enabled. (code: software/kohakutpu/compiler/crates/ktpu-l1/src/layout/tensor.rs:79-95, :115-136, :249-259; software/kohakutpu/compiler/crates/ktpu-l2/src/lower/mover/packing/image.rs:8-15; software/kohakutpu/compiler/crates/ktpu-l2/src/lower/mover/packing/finish.rs:27-48; evidence: No existing named RTL or card witness established.)

## Accumulator

- **NUM.ACC.001** `silicon` Accumulator representation is S1 E7 M`mg.acc_mantissa_bits` [14], with `mg.acc_bits` [22] total bits: low fraction bits, next seven exponent biased 63, highest sign. Normalized nonzero results have implicit leading one; zero normalization emits all-zero bits and addition treats exponent zero as zero. (code: src/kohakutpu/matmul/mx_acu_fp.v:54, :94-95; src/kohakutpu/matmul/mx_fpacc.v:247-276, :315-319; evidence: Card v9t6 GEMMs witness accumulator format.)
- **NUM.ACC.002** `silicon` A K block contains mg.kblock [32] products. Its integer partial is scaled by (8+Ma)*(8+Mb) and exponent ea+eb-anchor-6, then normalized with mg.acc_mantissa_bits [14] fraction bits. Loading selects normalized block; subsequent ADD selects resident accumulator and normalized block as float-add operands. (code: src/kohakutpu/matmul/mx_acu_fp.v:157-162, :183-198, :215-221, :315-326, :364-371; machines/multimesh_v9t6.json:187-191; evidence: Card v9t6 GEMMs cover this accumulator path.)
- **NUM.ACC.003** `unproven` Addition right-shifts smaller significand, records discarded nonzero bits as lost, adds/subtracts truncated values, normalizes and increments on guard with sticky or odd retained bit. Sticky includes lost for both signs; subtraction does not subtract discarded residue. Zero inputs pass through or yield zero; nonpositive output exponent flushes and exponent >=127 saturates to signed largest encoding. (code: src/kohakutpu/matmul/mx_fpacc.v:315-362, :385-395, :422-455; evidence: tests/matmul/mx_acu_fp_tb.v:244-269 checks FP16-tolerance accumulation, not exact subtraction rounding boundary.)
- **NUM.ACC.004** `silicon` Accumulator FP16 conversion rounds mg.acc_mantissa_bits [14] stored fraction bits to ten with nearest-even rounding, rebiases exponent from 63 to 15 and increments exponent on carry. (code: src/kohakutpu/matmul/mx_fpacc.v:581-601, :609-620; src/kohakutpu/matmul/mx_acu_fp.v:634-637, :664-666; evidence: Card tally fn.drain 314; tests/matmul/mx_fpacc_tb.v:254-285 additionally checks the parameterized converter with MW=16.)
- **NUM.ACC.005** `RTL` Accumulator FP16 conversion rounds the fraction and adjusts for carry before flushing a resulting FP16 exponent <=0 or saturating exponent >=31. Saturation produces signed 65504, and positive underflow flushes to zero; no FP16 subnormal or exponent-31 encoding is emitted. (code: src/kohakutpu/matmul/mx_fpacc.v:605-630; evidence: tests/matmul/mx_fpacc_tb.v:34, :254-292 checks converter carry, positive/negative saturation and positive flushing with MW=16.)
- **NUM.ACC.006** `RTL` SEND transfers mg.lanes squared [16] values in mg.acc_bits [22]-bit accumulator format. ADD_PEER adds peer to resident accumulator without FP16 conversion; emission converts to FP16. Retained partial reductions therefore need only final FP16 conversion, though accumulator adds still round. (code: src/kohakutpu/matmul/mx_acu_fp.v:76-80, :364-373, :631-637, :651-665; machines/multimesh_v9t6.json:187-190; evidence: tests/matmul/mx_acu_fp_tb.v:290-315 checks SEND/peer feedback/ADD_PEER/emission; bench parameterizes width, default differs from v9t6.)
- **NUM.ACC.008** `unproven` The K-pair pump compares exponent sums and right-shifts the smaller-exponent magnitude by the absolute difference capped at VWM, without testing whether the larger-exponent magnitude is zero. It adds or subtracts aligned magnitudes by sign; overflow causes another right shift and exponent increment. A zero partial can force truncation of its nonzero partner. (code: src/kohakutpu/matmul/mx_acu_fp_pump.v:339-382; evidence: No named current witness established.)
- **NUM.ACC.007** `unproven` The accumulator-specific host FP16 decoder interprets exponent zero as signed zero and exponent 31 as an ordinary binade rather than infinity or NaN. The general IEEE decoder retains subnormals and exceptional encodings. Saturation has no distinct result encoding. (code: software/kohakutpu/compiler/crates/ktpu-isa/src/numerics.rs:90-99, :131-142; src/kohakutpu/matmul/mx_fpacc.v:622-629)

## Layouts: sub-tiles, GT4, statistics

- **NUM.GT4.001** `silicon` A sub-tile is mg.lanes [4] squared f16 values in die.word_bytes [32] bytes, element (i,j) at index 4*i+j. For tile=(gm,gn), sub-tile coordinate (r,c) has byte offset ((floor(r/gm)*floor(N/(4*gn))+floor(c/gn))*gm*gn+(r mod gm)*gn+c mod gn)*die.word_bytes. Current address code uses literal four. (code: software/kohakutpu/compiler/crates/ktpu-l2/src/lower/memory.rs:192-198; src/kohakutpu/matmul/mx_acu_fp.v:184-198, :634-637; evidence: Address formula matches af7c1bb:software/compiler/kohakutpu/compiler/l2/memory.py:114-121; card mm drained buffers witness it.)
- **NUM.GT4.002** `silicon` Mover transform id 2 transposes four die.word_bytes [32]-byte words as four-by-four die.word_bytes/4 [8]-byte granules: output word i granule w is input word w granule i. Four row-major FP16 words become four four-column sub-tiles; applying twice restores input. (code: src/kohakutpu/transform/xform_bank.v:59-77, :99-113, :123-133; evidence: Card tally fn.gt4 442.)
- **NUM.GT4.003** `silicon` Vector GT4 transposes each four-word group before f16 unpack; each granule holds four f16 values. With vc.lanes [16] and vc.chunks [8], a register holds four rows by vc.vlmax/mg.lanes [32] columns; row i columns 16*q..16*q+15 occupy chunk 4*q+i on v9t6. (code: src/kohakutpu/vector2/v2_core.v:801-804, :851-857; src/kohakutpu/vector2/v2_gt4.v:66-94, :103-120; machines/multimesh_v9t6.json:206-208; evidence: Card tally GT4 vunpk 450.)
- **NUM.GT4.004** `unproven` The V2 core faults F_GT4 for a GT4 pack or unpack whose decoded chunk count is not divisible by four. (code: src/kohakutpu/vector2/v2_core.v:469, :519-533; evidence: No named fault test established.)
- **NUM.STAT.001** `unproven` Row statistics store one f16 per row, vc.lanes [16] per die.word_bytes [32]-byte word. Generator output storage covers the larger of its declared row extent and offset+R rounded up to vc.lanes; planning uses vc.lanes row quantum for statistic operands/output. Trips can contain multiple quanta, not necessarily sixteen rows. Statistic operands load one-dimensional row slices. (code: software/kohakutpu/compiler/crates/ktpu-kernels/src/gen/mod.rs:73-74, :97-105; software/kohakutpu/compiler/crates/ktpu-kernels/src/gen/vector.rs:178-185, :445-460, :925-929, :1177-1197; evidence: No named current RTL witness of complete Rust generator path established.)
- **NUM.STAT.002** `unproven` Tinygrad rows reduction padding uses 0.0 for sum, negative infinity for max and 1.0 for product. (code: software/kohakutpu/application/kohakutpu/application/ktpugrad/run/reduce.py:295; evidence: implementation inspection only; no card or named RTL witness established for this complete contract.)

## SRU row-sum error budget

[RUL.098](../rulings.md#sru-accumulation-precision-lead-ruling-2026-10-24)
defines R2 precision. This derived bound adds to upstream function/FMA/input
conversion errors. It compares the sum S of normal finite E8M15 inputs, treating
all exponent-zero encodings as zero, with accumulator T before conversion.
If S instead includes mathematical subnormal values, add their absolute values.
(code: src/kohakutpu/sru/stream/sru_red.v:158-162, :192-199)
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
corresponding rho(S) as well. This is a derived SRU bound, not a compiler-propagation guarantee.
The tinygrad diagnostic uses gamma-based E8M15, FP22, CPU-f32 and storage
allowances rather than this word-ordered SRU bound. Neither changes bit-exact
bench expectations. (code: src/kohakutpu/sru/stream/sru_sum2e8.v:85-122;
software/kohakutpu/application/kohakutpu/application/checks/kernels.py:174-191)

## Lowered tinygrad reduction error budget

A tinygrad schedule AST is a logical kernel, not necessarily one arithmetic
format or one launch. Its initial `vector` plan classification does not rule
out MXFP7 execution. The following lowering rules describe software structure;
they do not upgrade the evidence labels on the numerical format rules above.

- Vector-path selection considers recognized reduction windows, tabled results
  and enumerated phases before ordinary contraction, epilogue materialisation
  and plain-window fallback. A logical vector plan can therefore execute cluster
  products and need MXFP7 error allowances. (code: software/kohakutpu/application/kohakutpu/application/ktpugrad/run/reduce.py:271-290; software/kohakutpu/application/kohakutpu/application/ktpugrad/run/kernels.py:340-457)
- Product A may use dense f16 activation or an MXFP7 form; B uses MXFP7. Cluster
  emission converts accumulator values to f16 before the vector epilogue;
  same-package execution does not remove that boundary. (code: software/kohakutpu/application/kohakutpu/application/ktpugrad/run/product.py:275-338; src/kohakutpu/matmul/mx_acu_fp.v:631-637, :651-665)

- The explicit Python column fallback halves rows, stores f16 partials and
  carries an odd final row. With single-package reduction enabled it selects a
  generated kernel instead; its arithmetic/storage boundaries cannot be inferred
  from the fallback's loop count. (code: software/kohakutpu/application/kohakutpu/application/runtime/tree.py:100-141, :171-214)
- Rows execution first admits a whole-expression, whole-row band where it fits,
  avoiding external block partials. Otherwise it may materialise inner statistics
  or emit f16 block partials and folds. Single-package composition does not by itself
  change partial precision. (code: software/kohakutpu/application/kohakutpu/application/runtime/rows.py:194-221, :240-322)

- Materialisation computes an inner reduction into f16 temporary then uses it
  in the remaining expression; rows can select it after reduction splitting.
  Narrowing similarly materialises a non-reduction expression to reduce reads.
  The diagnostic marks observed narrowing uncertifiable. (code: software/kohakutpu/application/kohakutpu/application/ktpugrad/run/kernels.py:92-173, :469-474; software/kohakutpu/application/kohakutpu/application/checks/path.py:148-160)

For a finite, non-overflowing sum with ideal summands `t_i`, propagated
summand errors `e_i`, and at most d f16 storage boundaries on any summand's
path, storage error is bounded in **pre-cancellation absolute magnitude**:
for `d*u16 < 1`, use `A = sum(|t_i| + e_i)` and `gamma_d(u16) A`, not
`gamma_d(u16) |sum(t_i)|`, where `gamma_n(u)=n*u/(1-n*u)` and `u16=2^-11`.
An RNE subnormal f16 store also contributes at most `2^-25` absolute error per
term/boundary, propagated through later operations. Cluster stores instead
need flushing allowances; the diagnostic adds depth * 2^-14 when cluster blocks
are observed and uses infinity when n*u >= 1. (code: software/kohakutpu/application/kohakutpu/application/checks/kernels.py:49-50, :174-191)
Arithmetic and storage
factors compose multiplicatively. Include the independently executed CPU
f32 reduction rounding (`u32=2^-24`) when using it as the comparison value.
Use an absolute bound for MXFP7 perturbations too:
`|delta(ab)| <= |a| e_b + |b| e_a + e_a e_b`.
The half-scale and reciprocal-table allowances use the magnitude of the
**actual converted operand**; a computed temporary cannot in general be
bounded by its source buffer's peak. Saturation, nonfinite input, or an
unrepresented intermediate prevents a finite rounding-only certificate.

ArithmeticPath wraps selected runtime calls and records arguments and inferred
format/stage descriptions. Its depth is the maximum of counted arithmetic
launches, rows/flat/product records and inferred depths; matmul counts twice.
Column-tree order/depth is inferred from row count, not traced inside generated
kernels, so records do not establish exact internal serial stage counts.
(code: software/kohakutpu/application/kohakutpu/application/checks/path.py:87-112, :196-207, :213-246;
software/kohakutpu/application/kohakutpu/application/runtime/tree.py:100-141)

Narrowing and recognized computed-product operands are marked uncertifiable.
Detection uses tmp/narrow keys and earlier materialisation, not complete operand
provenance. Any recorded reason makes all budgets infinite.
(code: software/kohakutpu/application/kohakutpu/application/checks/path.py:37-46, :133-160;
software/kohakutpu/application/kohakutpu/application/checks/kernels.py:279-282)

Budgets use independent CPU AST/input/path metadata before output comparison.
Gamma-based reduction/storage allowances and heuristic seed allowances are a
diagnostic policy, not certified hardware accuracy bounds.
(code: software/kohakutpu/application/kohakutpu/application/checks/kernels.py:107-113, :174-203, :225-232, :297-308;
software/kohakutpu/application/kohakutpu/application/checks/step.py:141-159)

## Tolerances

`rel(got, want) = max|got - want| / max|want|` in float64 (want floored at
1e-9 in the runtime tests).

- **NUM.TOL.001** `unproven` Numerical checks use distinct policies. The tinygrad stage fixture accepts maximum absolute error no greater than tolerance times max(maximum absolute reference, 1e-3). The kernel diagnostic instead requires finite results and finite nonnegative budgets and checks every absolute error against its propagated budget; its exact category compares f16 bytes including signed zero. Reported diagnostic relative errors use a 2^-14 floor, not its acceptance threshold. (code: software/tests/stages/tinygrad/conftest.py:37-43; software/kohakutpu/application/kohakutpu/application/checks/kernels.py:21, :311-357; evidence: software/tests/stages/tinygrad/conftest.py:26-30 selects the device double, not RTL.)
- **NUM.TOL.002** `RTL` The named Verilated unit anchor compares contraction and drain-add outputs against the device double with maximum absolute error no greater than eight times the largest reference f16 spacing. It does not impose a general 1e-2 quantised-reference bound across split K, data-dependent tiles and shared fills. (code: software/tests/anchors/test_units.py:19-42, :45-57; evidence: test_units_end_as_the_double_says at software/tests/anchors/test_units.py:51-57.)
- **NUM.TOL.003** `unproven` The stack does not provide the named two-layer activation-requantisation rel < 2e-2 guarantee. The checker includes MXFP7 input error for matmul or MXFP7 lowering, propagates it through an error expression and compares per-output absolute bounds rather than fixed inter-layer relative error. (code: software/kohakutpu/application/kohakutpu/application/checks/kernels.py:279-308, :311-336; evidence: Named test_l2_mlp_quantises_its_activation_on_the_core and test_l2_mlp_with_nothing_in_memory_between_layers have no current implementations.)
- **NUM.TOL.004** `unproven` DSL activation, softmax, layernorm and GLU stage tests compare L2 simulation with graph evaluation using maximum absolute error <= 1e-3 * max(maximum absolute reference,1e-3). They do not establish operation-specific hardware tolerances or cluster-to-core SiLU tolerance. (code: software/tests/stages/dsl/test_kernels.py:30-32, :40-69, :75-97; evidence: Tests use simulate_l2, not RTL or Verilator.)
- **NUM.TOL.005** `unproven` The DSL example registry sets tolerances 2e-2 for linear and splitk and 3e-2 for mlp, swiglu, attention and heads. References apply _mx to operands and intermediate activations as specified by each function. Registry thresholds alone do not establish passing hardware accuracy. (code: software/kohakutpu/language/kohakutpu/language/examples/dsl/__init__.py:124-152, :174-216)
- **NUM.TOL.006** `unproven` The checker requires identical f16 storage for its exact class. DSL stages compare padded-head quantisation bytes to reference packing and rotated-run relayout to exact arrays on the L2 simulator. These do not establish blanket hardware guarantees for movement, quantiser bytes and GT4. (code: software/kohakutpu/application/kohakutpu/application/checks/kernels.py:53-76, :311-336; software/tests/stages/dsl/test_kernels.py:100-141, :178-201; evidence: test_padded_heads_check_their_finite_status_once and test_rotated_runs_gather_their_source_words_once use simulate_l2; legacy test_l2_mover_quantises_and_tiles is absent.)
- **NUM.TOL.007** `unproven` Tinygrad stage tests execute the tensor program on the device double with f16 inputs and independently on CPU with f32 inputs. Default tolerance is 2e-3 against maximum reference magnitude floored at 1e-3; cases may override it. (code: software/tests/stages/tinygrad/conftest.py:23, :26-45; software/tests/stages/tinygrad/test_ops.py:73-84)
- **NUM.TOL.008** `unproven` Stage product tests set tolerance per case: transposed weights use MX7=0.02, a product followed by reshape and arithmetic uses 2*MX7 and the tested GELU epilogue uses 4*MX7. They compare device-double f16 execution with independent CPU f32 under NUM.TOL.007; these thresholds are not universal product error bounds. (code: software/tests/stages/tinygrad/test_ops.py:12-13, :79-84, :143-195; software/tests/stages/tinygrad/conftest.py:37-43)
- **NUM.TOL.009** `unproven` Offline kernel checking records lowering/launches with passed=None and status shape_only_not_executed, without numerical comparison. The stack does not provide the named offline-plan allclose guarantee with rtol 3e-2 and atol 3e-2 times largest reference magnitude. (code: software/kohakutpu/application/kohakutpu/application/checks/step.py:73-106; evidence: No current test_plans_compute implementation found.)
- **NUM.TOL.010** `unproven` MNIST reports training/evaluation metrics without fixed LeNet loss or accuracy assertion. Optional final checking compares forward logits to CPU f32 and simulated-MXFP7 using final card weights, not independent CPU updates. CPU reference training uses AdamW with b2=0.95; accelerator uses Adam16. (code: software/kohakutpu/application/examples/tinygrad/mnist_mlp.py:157-175, :180-199, :287-293, :328-329; evidence: af7c1bb:software/application/examples/tinygrad/mnist_mlp.py:175-198 trains separate CPU Adam from initial weights; :80-100 samples with replacement, differing from current recipe.)

## Rulings

The disagreements found on this page are ruled in `../rulings.md`: RUL.019, RUL.056, RUL.057, RUL.058, RUL.059, RUL.060, RUL.061, RUL.062.
