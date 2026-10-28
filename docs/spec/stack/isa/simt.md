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
| `simt.lanes` | — | 8 | integer lanes (threads) a wave; 8 x 32 bit = one 256-bit flit payload | `docs/projects/kohakumpe/simt/README.md:99-110`; `status.md:31` |
| `simt.waves` | — | 16 | resident waves | `status.md:31` |
| `simt.flanes` | — | 8 | binary32 FMA units; RTL default 0 (no float tier) | `status.md:31-36` |
| `simt.fsfu_units` | — | nonzero | seed units (exp2, log2, rcp, rsqrt); RTL default 0 | `status.md:33-36,48` |
| `simt.shfl_units` | — | `simt.lanes` | subgroup butterfly width; 0 faults `shflxor` / `bcast` | `isa.md:251-256` |
| `simt.lds_banks` | — | 8 | banked shared-memory banks | `status.md:93-111` |
| `simt.ipdom_d` | — | — | divergence stack depth D (D/2 nested levels) | `README.md:197,199` |

## Clauses

- **ISA.SIMT.001** `unproven` Owner rule: integers are allowed only on the system node and on SIMT units; every tensor on the card is float (NUM.MEM.001). A SIMT program may hold integer per-thread values; nothing it writes to a tensor the rest of the stack reads is an integer. (doc: owner scope rule, stated in this clause)
- **ISA.SIMT.002** `unproven` The stack drives a SIMT unit only on a machine whose schema lists the unit kind with `simt.count` > 0; today no machine does, and every SIMT program is refused. (code: `ktpu-l1::lower::target` (no SIMT kind); doc: owner rule: unit kinds, firmware features, mesh shape, node and core counts are machine-schema configuration, never constants in the stack ([../machine.md](../machine.md)))
- **ISA.SIMT.003** `unproven` A SIMT unit is a compute unit on one mesh port: a shader image arrives as a burst into its instruction window, a kick starts it, and completion returns the same way as any unit's; a halt flushes before it completes, so a completion means the unit's stores are in memory. (doc: `README.md:14-18,30-37,198`)
- **ISA.SIMT.004** `unproven` The kick's operation field is the wave count, clamped to `simt.waves`; the kick argument is readable as control slot 0 and the wave id as slot 5. (doc: `status.md:73-76`; `isa.md:312-322`)
- **ISA.SIMT.005** `unproven` Ordinary RV32I encodings address the per-thread register file (`x0..x31` per lane per wave); the scalar file (`s0..s31` per wave) and all control flow are in custom-2 (0x5B, R-type groups by `funct3`) and custom-3 (0x7B, I-type, one instruction a `funct3`): 136 custom instructions, 128 on custom-2 and 8 on custom-3, plus RV32M at its standard encoding. (doc: `isa.md:20-35,41-75`; `docs/arch/cpu/rv32-pe/opcode-map.md:31-34`; RTL `src/kohakumpe/simt/generated/kht_isa.vh`, generated from `tests/pe/tools/rv_simt_isa.py`)
- **ISA.SIMT.006** `unproven` The groups are SALU (custom-2 f3 0, 10), SMOV (f3 1, 3: `s2v`, `rdctl`, `sld`), DIV (f3 2, 4: `split`, `join`, `tmc`, `bar`), SUB (f3 3, 10: shuffle, broadcast, ballot, five reductions, `vreadfirst`, `vlaneid`), VMEM (f3 4, 80), FLT (f3 5, 8), VX (f3 6, 7), STRM (f3 7, 6), and custom-3 f3 0..7 (scalar immediates, two uniform branches). (doc: `isa.md:24-35`)
- **ISA.SIMT.007** `unproven` The only float format is IEEE binary32 in, compute and out; denormals flush to a sign-preserved zero on input and output; there is no int-float conversion. FLT is `vfma` (vd = vs1 * vs2 + vd), `vfmul`, `vfadd`, `vfsub` and the seeds `vfexp2`, `vflog2`, `vfrcp`, `vfrsqrt`. (doc: `isa.md:94-131`; `README.md:132-146`; `status.md:50`)
- **ISA.SIMT.008** `unproven` A feature built at zero faults rather than computing: FLT at `simt.flanes` 0, the seeds at `simt.fsfu_units` 0, `shflxor` / `bcast` at `simt.shfl_units` 0; divide and remainder, atomics (the A major), and RV32I conditional branches in shader code raise illegal-instruction faults. (doc: `README.md:200`; `isa.md:105-106,154-182,251-256`)
- **ISA.SIMT.009** `unproven` VX (`vfmin`, `vfmax`, `vflt`, `vfle`, `vfeq`, `vcvtfh`, `vcvthf`), STRM (`ldsfill`, `ldsdrain`, `ldssend`, `credit`, `wait`, `cntset`), the LDS atomics `vamo*` and `sld` are specified and fault in the RTL until built; the stack emits none of them. (doc: `isa.md:32-39`; `status.md:195-198`)
- **ISA.SIMT.010** `unproven` `bar` encodes, does not execute and does not fault: it retires as a no-op. The stack emits no `bar`. (doc: `isa.md:208-220`; `status.md:248-251`)
- **ISA.SIMT.011** `unproven` Divergence: `split vs1` pushes two stack entries, `join` pops one and resumes at its own pc + 4, `tmc ss1` sets the active mask (zero retires the wave); a stack of depth D permits D/2 nested levels; overflow and underflow fault. An all-zero active mask is never issued. (doc: `isa.md:184-206,242-244`)
- **ISA.SIMT.012** `unproven` Addressing: lane-linear (`vlin`/`vsin`, one request), uniform base plus per-lane offset (`vl`/`vs`, `s[ss1] + (v[vs2] << scale)`, 1..`simt.lanes` requests), and RV32I `lw`/`sw` with a per-lane base (1..`simt.lanes` requests). Offsets are signed; `s + (v << scale)` wraps at 32 bits. VMEM `funct7 = op << 4 | scale << 2 | width`. (doc: `isa.md:258-300`)
- **ISA.SIMT.013** `unproven` A per-lane access to an unmapped region halts the unit with the region-fault cause at the faulting program counter. (doc: `status.md:136`)
- **ISA.SIMT.014** `unproven` One instruction is in flight at a time and a cache miss holds the whole front end; the coalescer and multiple outstanding misses are not built. (doc: `status.md:70-71,89-91,226-246`)

## Rulings

The disagreements found on this page are ruled in `../rulings.md`: RUL.015, RUL.019, RUL.055.
