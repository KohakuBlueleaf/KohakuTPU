# What the compiler path cannot do yet

The gap between `ktpu.ir` / `ktpu.codegen` and what actually runs on the card.
Written so the next person fills it deliberately rather than discovering it.

**Nothing here is silently wrong on hardware today**, because `fromdsl.flits`
refuses to emit anything it cannot handle. The cost is that the compiler path is
unusable for real work, and every kernel goes through the hand-built
`ktpu.hw.chain` path instead.

---

## 1. The headline: nine of twelve opcodes cannot be encoded

`ktpu.ir.program.Opcode` has twelve members. `ktpu.codegen.encode.OPCODE` maps
three:

```python
OPCODE = {Opcode.FILL: 1, Opcode.GEMM: 2, Opcode.DRAIN: 3}
```

| opcode | engine | encodable |
|---|---|---|
| `FILL`, `GEMM`, `DRAIN` | matmul cluster | **yes** |
| `VSETVL`, `VSETMD`, `VSETI` | vector core | no |
| `VLOOP`, `VLD`, `VST` | vector core | no |
| `VALU`, `VRED` | vector core | no |
| `HOST` | host | no |

So the compiler can express a vector program in its IR, lower it, schedule it,
and then not emit it.

## 2. This is not a missing table entry

The obvious reading -- "add nine rows to `OPCODE`" -- is wrong, and it is worth
saying why before someone tries it.

**A vector instruction is not a flit.** `ktpu.hw.vector`, the driver-side
encoder that does work today, documents two nested encodings:

```
    IMEM  [255:252]=1  [251:243] addr   [31:0] word
    DESC  [255:252]=2  [251:249] ad     [248:246] fld  [245:212] value
    RUN   [255:252]=3  [251:243] start pc
```

The 32-bit vector instruction word is *cargo*. It rides inside a CU_INST
envelope that says "write this into instruction memory at this address". A
vector kernel is therefore **a program, not an instruction**: N x `IMEM` to load
the body, M x `DESC` to set descriptors, then one `RUN`.

`codegen.flit()` maps one `Inst` to one flit. Vector support needs a different
shape -- one IR instruction expanding to a variable number of flits, plus
instruction-memory address allocation, plus a `RUN` to enter.

**And the opcode space is shared.** Envelope `1/2/3` are IMEM/DESC/RUN at a
vector node and FILL/GEMM/DRAIN at a cluster node. The same four bits, resolved
only by the destination node. So the encoder cannot be node-agnostic: it must
know what kind of node each instruction targets, and a mistake there is silently
executed rather than rejected.

## 3. `fromdsl.flits` handles exactly one program shape

```python
cluster = [i for i in prog.insts if i.op in _CLUSTER_OPS]
if len(cluster) != 4:
    raise ValueError(
        f"{len(cluster)} cluster instructions; this relocation handles the "
        "single-pass case (fill, fill, gemm, drain) only"
    )
```

Anything that is not exactly `fill, fill, gemm, drain` is refused. That covers:

- multi-pass GEMMs (any K needing more than one chunk),
- anything with a vector band in it,
- consequently **the on-device relayout band** that
  [`../limits.md`](../limits.md) s6.6 relies on to chain matmul into matmul.

This is the direct reason the SDXL blocks still perform host repacks -- 108 of
them in the transformer block. The relayout is a vector multiply by 1.0, the
hardware supports it, `passes.lower` inserts it, and it cannot be staged.

It also hardcodes operand relocation for three regions (`v0`/`v1`/`v2`) and
stamps `preq`, `emit` and `fuse` by opcode. Those are single-pass assumptions,
not general ones.

## 4. Two encoders, one of them unreachable

| | `ktpu.codegen` | `ktpu.hw.vector` |
|---|---|---|
| cluster ops | yes | via `ktpu.hw.matmul` |
| vector ops | **no** | **yes, and runs on the card** |
| input | `ktpu.ir` `Inst` | `veckernels.Asm` programs |
| field-width checks | yes (`EncodeError`) | yes |

The vector encoding problem is **solved** -- it is just solved in the driver,
against a different input type, and not wired to the IR. Filling the gap is
substantially a matter of teaching `codegen` to produce what `hw.vector` already
produces, not of inventing an encoding.

`tests/ktpu/integration/test_machine_code.py` already compares ktpu's flits to
`ktpu.hw.bench`'s byte for byte for the cluster path. The same relationship is
what the vector path needs.

## 5. The banking bug was here too, and worse

Fixed 2026-08-11, recorded because it shows the two paths drift independently.

`codegen.encode.FIELDS` had no `abank`/`bbank`/`fbank` at all, and `codegen/cu.py`
computed B's offset as `ch * b_ent` -- which does not merely truncate at 256, it
**addresses past two banks entirely**. Adding the bank fields and a range check
immediately failed 25 tests on `L1 entry 512 is in bank 2`: the path had been
relying on 8-bit wrap for shapes that do not fit L1 at all. A capacity bug
wearing a truncation bug's clothes, completely silent.

A was safe only by accident -- `(ch % 2) * a_ent` alternates, so it stayed inside
one bank while `a_ent <= 128`.

Fixed by mirroring the legacy planner's residency rule. The cost is B refills on
shapes that never really had residency: more traffic, correct answers.

## 6. What to build, in order

1. **A vector program emitter in `codegen`** -- expand one IR vector instruction
   into its `IMEM` / `DESC` / `RUN` envelope sequence, allocate instruction
   memory, and emit a `RUN`. Check it byte-for-byte against `ktpu.hw.vector` for
   a kernel both can express, the way the cluster path is checked against
   `ktpu.hw.bench`.
2. **Node-type awareness in the encoder** -- an instruction must know whether it
   targets a cluster or a vector core, because the opcode space is shared and a
   misdirected flit executes rather than faults.
3. **Generalise `fromdsl.flits`** -- multi-pass programs, mixed cluster/vector
   bands, and relocation that is not three hardcoded regions.
4. **Decide what `HOST` means.** Either the compiler emits host callbacks as
   part of a schedule, or it refuses programs containing them. Right now it does
   neither.

(1) and (3) together are what remove the host repacks. That is the measurable
prize: 3.84 GiB per SDXL forward is a cost of the hand-built path, not of the
hardware.
