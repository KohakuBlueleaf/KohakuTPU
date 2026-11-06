---
title: ISA: SIMT units
summary: The SIMT PE (`src/kohakumpe/simt/`, top `kht_pe`) as a unit kind the stack may drive.
tags:
  - spec
  - stack
  - isa
---
# ISA: SIMT units

The SIMT PE (`src/kohakumpe/simt/`, top `kht_pe`) as a unit kind the stack
may drive. **Every clause on this page is `unproven`.** No KohakuTPU die or
board carries a SIMT unit (the v9t6 die is two clusters and two vector
cores; `sru_v9t7` adds SRUs; no board file names SIMT), and nothing in
`software/` emits, encodes, verifies or dispatches SIMT code: the compiler's
unit kinds are `MG`, `VC` and `SR`
(`ktpu-l1::lower::target`). The SIMT
mesh card is parked on the owner's hardware track.

The PE has its own RTL benches and golden model
(`python tests/pe/tools/rv_simt_suite.py --gates`,
`docs/projects/kohakumpe/simt/status.md:113-154`). Those runs prove the PE
against its model, not any path through this stack, so they are cited as
the definition's source and not as evidence for a stack clause.

## Parameters

| parameter | v9t6 | configuration of record | meaning | source |
|---|---|---|---|---|
| `simt.count` | 0 | — | SIMT units a die | no die top or board names one |
| `simt.lanes` | — | 8 | integer lanes (threads) a wave; 8 x 32 bit = one 256-bit flit payload | `docs/projects/kohakumpe/simt/README.md:99-110`; `simt/status.md:31` |
| `simt.waves` | — | 16 | resident waves | `simt/status.md:31` |
| `simt.flanes` | — | 8 | binary32 FMA units; RTL default 0 (no float tier) | `simt/status.md:31-36` |
| `simt.fsfu_units` | — | nonzero | seed units (exp2, log2, rcp, rsqrt); RTL default 0 | `simt/status.md:33-36,48` |
| `simt.shfl_units` | — | `simt.lanes` | subgroup butterfly width; 0 faults `shflxor` / `bcast` | `simt/isa.md:251-256` |
| `simt.lds_banks` | — | 8 | banked shared-memory banks | `simt/status.md:93-111` |
| `simt.ipdom_d` | — | — | divergence stack depth D (D/2 nested levels) | `simt/README.md:197,199` |

## Clauses

- **ISA.SIMT.001** `unproven` SIMT memory stores copy register bit patterns; the store-data path does not distinguish integer values from floating-point tensors or enforce a float-only exchange boundary. L1 unit resolution recognizes only VC, MG and SR, so this stack does not provide a SIMT target on which to enforce the owner's float-tensor policy. (code: src/kohakumpe/simt/kht_core.v:1064-1069, :1411-1413; software/kohakutpu/compiler/crates/ktpu-l1/src/lower/target.rs:142-178; evidence: implementation inspection only)
- **ISA.SIMT.002** `unproven` L1 unit references recognize only `vc`, `mg` and `sr`. A SIMT unit reference is not resolved and is refused under L1.UNIT.001; this reader has no SIMT emission path. (code: software/kohakutpu/compiler/crates/ktpu-l1/src/lower/target.rs:120-178; evidence: host target-resolution implementation only)
- **ISA.SIMT.003** `unproven` The SIMT PE connects to the mesh through noc_cu_base. CU_DATA writes its instruction window; a nonzero kick launches execution after receive activity is quiet. Launched execution completes only after the core is halted, its pipeline is empty, the requestor is idle and its outstanding-write count is zero. Halt processing requests a memory-path flush and waits for its busy indication to rise and fall. Operation zero skips launch and its execution-wait path. (code: src/kohakumpe/simt/kht_pe.v:139-159, :205-233, :266-269, :324-385; src/kohakumpe/simt/kht_core.v:1417, :1441, :1711-1719; src/kohakumpe/simt/kht_req.v:73-74, :178-179; evidence: RTL implementation inspection only)
- **ISA.SIMT.004** `unproven` The kick operation field supplies the wave count; a nonzero count is clamped to `simt.waves` [no v9t6 SIMT instance]. Operation zero skips core launch and proceeds to completion. The kick argument is readable as control slot 0 and the current wave id as slot 5. (code: src/kohakumpe/simt/kht_pe.v:326-365, :429-432; src/kohakumpe/simt/kht_core.v:246-258, :735-744; evidence: RTL implementation inspection only)
- **ISA.SIMT.005** `unproven` The decoder recognizes custom-2 (0x5B) groups selected by funct3 and custom-3 (0x7B) scalar immediate and uniform-branch operations. Control flow is not confined to these majors: ordinary RV32I `jal` and `jalr` are accepted, with `jalr` using lane zero's base and clearing target bit zero. RV32I conditional branches and RV32M divide/remainder encodings are illegal; the four RV32M multiply encodings are recognized. The generated custom encoding inventory is not a list of operations implemented by the RTL (ISA.SIMT.009 and ISA.SIMT.010). (code: src/kohakumpe/simt/generated/kht_isa.vh:12-33; src/kohakumpe/simt/kht_predec.v:51-69, :97-105, :136-139, :207-208; src/kohakumpe/simt/kht_core.v:1652-1667; evidence: RTL implementation inspection only)
- **ISA.SIMT.006** `unproven` The assembler encoding inventory assigns custom-2 groups SALU (funct3 0, 10 encodings), SMOV (1, 3), DIV (2, 4), SUB (3, 10), VMEM (4, 80), FLT (5, 8), VX (6, 7), and STRM (7, 6). Custom-3 supplies six scalar-immediate operations and two uniform branches. This inventory is not the implemented operation set: the RTL rejects VX and STRM, and accepting SMOV or DIV does not implement SLD or BAR (ISA.SIMT.009 and ISA.SIMT.010). (code: tests/pe/tools/rv_simt_isa.py:127-141, :249-291, :299-398, :404-439, :450-509, :520-600, :604-644; src/kohakumpe/simt/kht_predec.v:34-54, :97-119; evidence: assembler table and decoder inspection only)
- **ISA.SIMT.007** `unproven` FLT uses binary32 operands and results: vfma computes vs1 * vs2 + vd through rv_fpu; vfmul, vfadd and vfsub select that unit's multiply, add and subtract forms. The four seed operations vfexp2, vflog2, vfrcp and vfrsqrt use khs_fp32_sfu. Exponent-zero inputs are treated as signed zero and finite output underflow is flushed to signed zero. The SIMT decoder and FLT operation map supply no integer-to-float or float-to-integer conversion instruction. (code: src/kohakumpe/simt/kht_predec.v:34-69, :97-105; src/kohakumpe/simt/kht_fpu.v:67-96, :138-172; src/kohakuaccel/pe/rv32/core/rv_fpu.v:45-64, :113-134, :295-324; src/kohakumpe/simd/khs_fp32_sfu.v:60-109, :339-374; evidence: RTL arithmetic-path inspection only)
- **ISA.SIMT.008** `unproven` The core faults FLT when `simt.flanes` is zero, seeds when `simt.fsfu_units` is zero, and `shflxor` or `bcast` when `simt.shfl_units` is zero [no v9t6 SIMT instance for these parameters]. It also faults an out-of-range broadcast source lane, TMC without mask hardware, and SPLIT/JOIN without mask and divergence-stack hardware. The decoder marks RV32M divide/remainder, the RISC-V A major, and RV32I conditional branches illegal. These checks feed the whole-unit fault path. (code: src/kohakumpe/simt/kht_predec.v:56-69, :97-105; src/kohakumpe/simt/kht_core.v:582-601, :1623, :1691-1696, :1736-1740; evidence: RTL implementation inspection only)
- **ISA.SIMT.009** `unproven` The decoder faults custom-2 VX and STRM groups. The SMOV group is accepted, but only `s2v` and `rdctl` produce their operation controls: `sld` has no scalar write or memory-request control and does not receive an illegal-instruction fault from this decoder or the core's unbuilt-feature checks. The L1 unit reader has no SIMT target. (code: src/kohakumpe/simt/kht_predec.v:34-42, :97-105, :118-119, :152-170, src/kohakumpe/simt/kht_core.v:593-601, software/kohakutpu/compiler/crates/ktpu-l1/src/lower/target.rs:142-178; evidence: implementation inspection only)
- **ISA.SIMT.010** `unproven` `bar` sets the decoder's C_BAR control but the core does not consume that control. It has no barrier effect and does not fault merely for being BAR; it retires as a no-op. The L1 target reader has no SIMT emission path. (code: src/kohakumpe/simt/kht_predec.v:97-105, :113-116, :152-170; src/kohakumpe/simt/kht_core.v:461-479, :593-601, :1732-1735; software/kohakutpu/compiler/crates/ktpu-l1/src/lower/target.rs:120-178; evidence: RTL implementation inspection and C_BAR consumer search only)
- **ISA.SIMT.011** `unproven` With mask and divergence-stack hardware enabled, SPLIT stores the outer and false masks as one pair and selects the true mask. JOIN first selects the false mask, then on the next JOIN selects the outer mask and releases the pair. Capacity is floor(IPDOM_D / 2) pairs, where IPDOM_D is the RTL divergence-depth parameter [no v9t6 SIMT instance]. Overflow and empty-stack underflow latch a fault. TMC replaces the active mask, including with zero; zero does not retire the wave or remove it from the ready set. A zero-mask issue produces a simulation diagnostic, not a hardware refusal. (code: src/kohakumpe/simt/kht_unit.v:271-305, :320-381, :1104-1108; src/kohakumpe/simt/kht_core.v:232, :279-285, :1691-1696, :1736-1758; evidence: RTL implementation inspection only)
- **ISA.SIMT.012** `unproven` VMEM extracts operation, scale and width from funct7 bits 6:4, 3:2 and 1:0. The implemented lane-linear predicate is operation >= LIN; its address is the scalar base plus the lane index shifted by scale. Otherwise VMEM uses the per-lane second vector operand as offset. RV32I memory operations use the per-lane first operand plus the instruction immediate. Address results wrap to 32 bits, so negative two's-complement offsets wrap by the same arithmetic. Lane-linear syntax does not guarantee one memory request: the queued LSU groups computed line addresses only when its COALESCE RTL parameter is enabled, and otherwise selects one pending lane. The L1 target reader has no SIMT emission path [no v9t6 SIMT instance]. (code: src/kohakumpe/simt/kht_predec.v:44-49; src/kohakumpe/simt/kht_core.v:481-485, :1047-1056; src/kohakumpe/simt/kht_lsu.v:252-268; software/kohakutpu/compiler/crates/ktpu-l1/src/lower/target.rs:142-178; evidence: RTL and host implementation inspection only)
- **ISA.SIMT.013** `RTL` A per-lane region-check access fault halts the whole unit. Region, illegal-instruction and divergence faults use completion cause 3; the halt word reports the selected fault program counter. The address-stage fault path reports its saved PC. (code: src/kohakumpe/simt/kht_core.v:1601-1623, :1736-1740, :1750-1758, :1766-1773; evidence: tests/pe/prog/simt_fault.s:15-25; tests/pe/prog/simt_aufault.s:11-23; tests/pe/tools/rv_simt_suite.py:60-73; tests/pe/tools/rv_simt_run.py:63-89,162-174)
- **ISA.SIMT.014** `unproven` The RTL has a configurable queued memory path: nonzero LSU_DEPTH enables the `kht_lsu` instance, which receives the COALESCE and MSHRS RTL parameters. With COALESCE enabled, it groups pending lanes whose computed line addresses match; otherwise it selects only the leading pending lane. It searches MSHRS fill buffers. The queued return path waits for the core when SCORE is zero and uses writeback readiness otherwise. These are RTL configuration controls, not a SIMT configuration emitted by this stack [no v9t6 SIMT instance]. (code: src/kohakumpe/simt/kht_core.v:149, :1347-1368, :1393-1424; src/kohakumpe/simt/kht_lsu.v:252-287; evidence: RTL implementation inspection only)

## Rulings

The disagreements found on this page are ruled in `../rulings.md`: RUL.015, RUL.019, RUL.055.
