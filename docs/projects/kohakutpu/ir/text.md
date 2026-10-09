# KohakuTPU's words in the IR texts

The texts' syntax and structure are the framework's
([docs/spec/ir-text.md](../../../spec/ir-text.md)); this page is what KohakuTPU
registers on them. Code: `compiler/kohakutpu/ir/l1/text.py`,
`compiler/kohakutpu/ir/l2/text.py`, `compiler/kohakutpu/ir/__main__.py`.
Every word is one hardware op and prints every field its encoding carries.

## 1. L1

**Clusters (`MG`).** A bank is its side and its index: `A0`, `B1`.

| word | op | fields |
|---|---|---|
| `fill A0 <- 0x8010_0000 n=128 l1=64` | `Fill` | address, entries, side and bank, L1 entry offset (`l1=`, omitted at 0) |
| `gemm 8x8x2 acc a=A0 b=B1 aoff=16 boff=4 emit=0x8010_3000` | `Gemm` | gm x gn x nk, `acc`, the banks read, operand offsets (omitted at 0), the fused-drain address (`emit=`) |
| `drain 0x8010_2000 fused n=64 to=(1, 2) flags=1 ack=(0, 1)` | `Drain` | address, `fused`, sub-tiles; a drain to a node's L1 names it, its flags and its ack target |

**Vector cores (`VC`).** What the node sends: `load IMAGE at WORD` (an `Image`,
its code a top-level `image NAME` block), `desc a1 0x8010_0000` (a base),
`dims a1 (1, 8) (32, 4)` (the walk, innermost first; none resets it), `run PC`.

**Vector-core instructions** (the lines of an `image` block). A source names its
selector: `v` register, `s` scalar, `c` chained, `k` constant.

| word | instruction |
|---|---|
| `vadd v3, v1, s2, k3 pr=1 pm=2` | any ALU opcode, lower case (`vmov`, `vexp2`, `vcmplt`, `vred`, ...): vd and the three sources as encoded, predicate register and mode (omitted at 0) |
| `vshuf v4, v3, s5 pr=2` | `VSHUF`: rotate by `S[srot]` |
| `vld v1, a1, 8 dt=f32` / `vst v4, a2, 0` | load/store through a descriptor at an offset; dtype `f16` (default) or `f32` |
| `vfill a1, 256` / `vdrain a3, 0 node=(2, 2) buf=1 signal=1` | L1 fill and drain through a descriptor at an L1 word |
| `seti s0, 128` / `seti k3, 0x3f_8000` | an immediate into a scalar or into K3 (a bit pattern: hexadecimal) |
| `setvl s0`, `setmode tree` (`flat d2 d4 tree`), `loop s1, 4`, `bar`, `halt` | |

**Mover.** `quantise SRC -> DST entries=N` (`Quantise`, FP16 -> MXFP7),
`copy SRC -> DST bytes=N` (`Copy`).

## 2. L2

Layouts, as `Name(field=value, ...)`: `MxA`, `MxB`, `Tiles`, `Flat`, `Rows`,
`BandLane`, `ConvB` ([l2.md](l2.md) §1).

| kind | unit | parameters (optional after `;`) |
|---|---|---|
| `gemm_tile` | MG | a_at b_at c_at gm gn nk chunks; late ones_at bias_at |
| `conv_tile` | MG | a_at a_chunk row0 wp b_at c_at gm gn cbc chunks; late ones_at bias_at |
| `gemm` | MG | a b gm gn nk c_at (`a`, `b` are `(address, entries, keep)`) |
| `vec_stream` | VC | body words runs step srcs dst; sink resident_at |
| `vec_run` | VC | gm run p16_at o_at idx_at; in_at |
| `quantise` | mover | src dst entries |
| `copy` | mover | src dst nbytes |

## 3. Machines

A text names its machine: `kohakutpu-l1` is the v9 die's four clusters and two
vector cores (`kohakutpu.ir.l1.model.MACHINES`).

## 4. Command line (`python -m kohakutpu.ir`)

| command | what |
|---|---|
| `check FILE...` | read and verify each text, by its `level` line |
| `fmt FILE` | the canonical text |
| `lower FILE.l2 [-o FILE.l1]` | the L2 -> L1 compiler, text in and text out |
| `build FILE.l1 -o DIR` | one L0 package a program, `DIR/NAME.pN.pkg`; a vector core's resident images carried across the file's programs |
