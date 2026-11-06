---
title: L0 binding and relocation
summary: How the addresses a package's words carry are chosen.
tags:
  - spec
  - stack
  - l0
---
# L0 binding and relocation

How the addresses a package's words carry are chosen: the buffer table and the
relocation entries the writer makes, the host binding a package before it is
sent (`Package.bind`), the firmware's refusal of relocations, and the
owner's rulings on card memory that bind time implements. The table layouts are
[package.md](package.md).

**What the card ran.** At commit `af7c1bb` (the 2026-10-21 v9t6 runs)
the package builder built every package with no address fields
(`Program.build(fetch, residents)`): the payloads carried the absolute
unit-global addresses the host had placed the buffers at, the package had no
buffers and no relocations, and the RUN entries carried no bindings. The
relocatable build (`Program.spans`, `ktpu_isa::addresses`), `Package.bind`
and the bound-package path came after those runs. Firmware refuses nonzero relocation counts before execution.

## Absolute packages

- **L0.BIND.001** `silicon` A package with no buffers and no relocations runs at the absolute unit-global addresses its payloads carry; the runtime submits it with `nbind` 0 and bindings address 0. (code: software/kohakutpu/application/kohakutpu/application/runtime/packages.py:234-244; software/kohakutpu/application/kohakutpu/application/runtime/card/submission.py:98-114; software/kohakuaccel/driver/kohakuaccel/driver/node/queue/client.py:815-864; software/kohakuaccel/firmware/package/interp.c:1529-1535; evidence: every 2026-10-21 card package)
- **L0.BIND.031** `unproven` Firmware does not resolve RUN bindings. A relocation-free package uses its submitted payload addresses regardless of RUN bindings; nonzero relocation count refuses before execution in production firmware. (code: software/kohakuaccel/firmware/package/interp.c:1525-1553; evidence: Code inspection; no qualifying card or named RTL witness established for this complete statement.)

## Making relocations (writer)

- **L0.BIND.002** `unproven` For each reported address field, PackageBuilder.payload selects the span with greatest base not exceeding the value, then checks that the value lies below that span's end. If that span already has a buffer, or the buffer-count gate admits resolving its synthesized buffer name, it emits one relocation per segment with that buffer and addend value minus base, and clears the segment. Otherwise the field remains absolute. It does not retry earlier overlapping spans. (code: software/kohakuaccel/compiler/crates/ka-package/src/build/mod.rs:223-275)
- **L0.BIND.003** `unproven` On first discovery of an eligible span, the builder requests a buffer named buf<i>, with i the current buffer count, passing the span size/base, TEMP kind and builder mesh. If that name already exists, buffer() returns the existing entry without replacing its metadata; otherwise it appends the new entry. The span then records that buffer index. (code: software/kohakuaccel/compiler/crates/ka-package/src/build/mod.rs:202-214, :250-259)
- **L0.BIND.004** `unproven` A split field (an address whose segments occupy separate fields) is one relocation per segment, same buffer and addend, its own bit, width and shift. (code: software/kohakuaccel/compiler/crates/ka-package/src/build/mod.rs:223-275; doc: docs/spec/package-format.md:147-150; evidence: as L0.BIND.002)
- **L0.BIND.005** `unproven` Once the smaller of 256 buffers (the relocation's 8-bit buffer field) and `fw.max_buffers` [64] exist, a field in a further span keeps its absolute value and makes no buffer. (code: software/kohakuaccel/compiler/crates/ka-package/src/build/mod.rs:148-151, :250-259)
- **L0.BIND.006** `unproven` bindings() returns the recorded buffer defaults in buffer-table order. In ordinary automatic span discovery without synthesized-name collisions these are the discovered span bases. (code: software/kohakuaccel/compiler/crates/ka-package/src/build/mod.rs:202-214, :250-259, :755-762)
- **L0.BIND.032** `unproven` `build(defaults=False)` writes every buffer's default address as 0. (code: software/kohakuaccel/compiler/crates/ka-package/src/build/mod.rs:724-752; doc: docs/spec/package-format.md:446-451; evidence: every test binds every buffer to a nonzero address, so no default is read)
- **L0.BIND.007** `unproven` With `build(defaults=False)`, changing span bases preserves package bytes only when the same fields select corresponding spans in the same discovery order, their addends and span sizes remain equal, and all remaining literal fields and metadata remain equal. Unmatched fields remain absolute; `build(defaults=True)` retains span bases in buffer defaults. (code: software/kohakuaccel/compiler/crates/ka-package/src/build/mod.rs:223-275, :724-752; evidence: Code inspection; no qualifying card or named RTL witness established for this complete statement.)
- **L0.BIND.008** `unproven` `MOVER` payloads relocate through the address-field function like any unit word: the mover walker header's base and the index base are their address fields. `STORE`, `LOAD`, `RECV` and `SEND MEM` address words (a pair's address at `[63:0]` of its 128-bit half, a `LOAD`, `RECV` or push address at `[63:0]`) relocate when the builder method is called with `relocate=True`. `Program.build` enables the shared memory-field rule for every framework memory operation. Control offsets, register indices, values, descriptors and opaque flits are not inferred to be allocation addresses. (code: software/kohakuaccel/compiler/crates/ka-package/src/build/mod.rs:237-275, :483-555, :613-686; software/kohakuaccel/compiler/crates/ka-package/src/program.rs:762-775, :822-840; software/kohakutpu/compiler/crates/ktpu-isa/src/reloc.rs:17-24; software/kohakuaccel/compiler/crates/ka-isa/src/mover.rs:779-798; doc: docs/spec/package-format.md:350-356; evidence: as L0.BIND.002: a test cannot tell a relocated field from an absolute one)

### External instruction addresses

A framework FETCH whose address lies in a declared allocation span stores
that address in a normal relocated 64-bit payload. In the host package,
FETCH's `F_MEM` flag means its whole argument is the address-payload index.
`Package.bind` reads the bound address into FETCH's argument and clears the
flag before submission or dispatch-engine lowering. The firmware therefore
receives the ordinary absolute FETCH encoding. A FETCH with no matching
span retains its literal argument; a zero-count FETCH emits nothing.

Runtime FETCH address PATCHes target the step argument, not its host address
payload. Binding refuses payload PATCHes of a host FETCH address. The host
index is checked at full width; nonzero high bits cannot alias payload 0.
The dispatch scheduler retains these host address sites when transferring
its ordered steps back to the unbound package.

## Host binding (`Package.bind`)

- **L0.BIND.009** `unproven` A buffer's address is `bindings[i]` when `i < len(bindings)` and that binding is not the `UNBOUND` sentinel. (code: software/kohakuaccel/compiler/crates/ka-package/src/format/bind.rs:13-24; evidence: current implementation inspected; no extant named RTL witness established for this statement.)
- **L0.BIND.033** `unproven` A missing binding or the `UNBOUND` sentinel takes the buffer's default address. Zero is a valid explicit address. (code: software/kohakuaccel/compiler/crates/ka-package/src/format/bind.rs:13-24; evidence: every test binds every buffer, to a nonzero address)
- **L0.BIND.010** `unproven` A relocation sets payload bits `[bit, bit + width)` to `((address + addend) >> shift) & (2^width - 1)` and leaves every other bit of the payload as it was. (code: software/kohakuaccel/compiler/crates/ka-package/src/format/bind.rs:94-117, :169-180; doc: docs/spec/package-format.md:142-145; evidence: current implementation inspected; no extant named RTL witness established for this statement.)
- **L0.BIND.011** `unproven` `Package.bind(bindings)` returns the package with every relocation applied to its payloads and with no relocations. Its units and steps are unchanged except that host FETCH address indirection is resolved to an absolute argument and its host flag is cleared. (code: software/kohakuaccel/compiler/crates/ka-package/src/format/bind.rs:293-333; evidence: current implementation inspected; no extant named RTL witness established for this statement.)
- **L0.BIND.034** `unproven` The bound package has no buffers or buffer names; its signature, ack_reserve and checksum flag are preserved. (code: software/kohakuaccel/compiler/crates/ka-package/src/format/bind.rs:322-333)
- **L0.BIND.012** `unproven` The runtime obtains submitted bytes from a cached launch recipe or from ordinary host binding with lowering enabled. The ordinary path calls `Prebuilt.bound(True, ...)`; the recipe path calls `recipe.launch(bindings, translations, cores)`. Both return an empty RUN binding list. (code: software/kohakutpu/application/kohakutpu/application/runtime/packages.py:147-171, :231-244; evidence: Code inspection; no qualifying card or named RTL witness established for this complete statement.)
- **L0.BIND.013** `unproven` `engine.lower` returns the package unchanged when any AWAIT has nonzero flags or any step is JUMP or PATCH. Otherwise a nonempty relocation table refuses with `L0.DE.039` before lowering. (code: software/kohakuaccel/compiler/crates/ka-package/src/engine.rs:351-354, :397-409; evidence: Code inspection; no qualifying card or named RTL witness established for this complete statement.)
- **L0.BIND.014** `unproven` A package whose program jumps or patches is bound on the host like any other; `engine.lower` returns it unlowered and the sysnode queues its steps itself ([dispatch-engine.md](dispatch-engine.md)). (code: software/kohakuaccel/compiler/crates/ka-package/src/engine.rs:351-354, :397-403; software/kohakuaccel/firmware/package/interp.c:1659-1716)

## Firmware binding boundary

The dispatcher refuses any nonzero relocation count with `BAD_RELOC`, detail
0 and value the count. It performs no binding or relocation. This RUL.022
boundary governs all firmware binding clauses below. Tier-1 vector: `RUL.022.card_refuses`; mutant image:
`kohakutpu_memory_mutant`. See [memory.md](memory.md).

- **L0.BIND.015** `unproven` Firmware does not resolve the RUN binding array. The runtime package preparation path resolves bindings on the host and returns an empty RUN binding list. (code: software/kohakuaccel/firmware/package/interp.c:1525-1534, :1779-1795; software/kohakutpu/application/kohakutpu/application/runtime/packages.py:147-171,231-244)
- **L0.BIND.016** `unproven` Relocation arithmetic belongs exclusively to the host binder; no firmware execution path applies a relocation. (ruling: RUL.022) (code: software/kohakuaccel/firmware/package/interp.c:1525-1534; software/kohakuaccel/compiler/crates/ka-package/src/format/bind.rs:169-180)
- **L0.BIND.017** `unproven` After magic and version/header-size validation passes, production firmware rejects a nonzero relocation count with BAD_RELOC (0x16), detail 0 and value the count, before any step executes. (code: software/kohakuaccel/firmware/package/interp.c:1518-1534)
- **L0.BIND.018** `unproven` The local-copy REPEAT path extracts each incremented field, adds repetition times delta, and reinserts the result masked to the field width. Carries outside the declared field do not alter adjacent fields. (code: software/kohakuaccel/firmware/package/interp.c:183-192, :321-346; evidence: implementation inspection only; no card or named RTL witness established for this complete contract.)
- **L0.BIND.019** `unproven` The firmware rejects relocations regardless of their ordering; it holds no relocation cursor. (ruling: RUL.022) (code: software/kohakuaccel/firmware/package/interp.c:1529-1534)
- **L0.BIND.020** `unproven` A local package copy contains the same bound payloads as the submitted package. There is no firmware pre-relocation pass. (ruling: RUL.022) (code: software/kohakuaccel/firmware/package/interp.c:114-136)
- **L0.BIND.021** `unproven` A package with relocations reaches neither fetch nor mailbox dispatch: header validation refuses it. (tier-1 vector: RUL.022.card_refuses) (code: software/kohakuaccel/firmware/package/interp.c:1529-1534, :1811-1814)

## Owner rulings on card memory (2026-10-21)

- **L0.BIND.022** `unproven` The dispatcher executes packages without allocating DRAM; the host reserves package and boot-region storage. The separately built heap-policy module is not a dispatcher allocation service. (code: software/kohakuaccel/firmware/queue/service.c:354-377; software/kohakutpu/application/kohakutpu/application/runtime/card/common.py:130-142; evidence: Code inspection; no qualifying card or named RTL witness established for this complete statement.)
- **L0.BIND.023** `unproven` The driver assigns non-reused virtual allocation ranges and records physical bases, extents and generations. Host binding resolves buffer handles through live translations before producing physical address fields; bound packages have no buffer or relocation table. (code: software/kohakuaccel/driver/kohakuaccel/driver/runtime/mmu.py:89-150; software/kohakuaccel/compiler/crates/ka-package/src/format/bind.rs:31-68, :169-180, :322-333; evidence: Code inspection; no qualifying card or named RTL witness established for this complete statement.)
- **L0.BIND.024** `unproven` This binding path is a host driver and host compiler interface; it does not implement a KohakuAccel OS kernel or an SMP software service. (code: software/kohakuaccel/driver/kohakuaccel/driver/runtime/mmu.py:89-150; software/kohakuaccel/compiler/crates/ka-package/src/format/bind.rs:31-68; evidence: Code inspection; no qualifying card or named RTL witness established for this complete statement.)
- **L0.BIND.025** `unproven` `CardMemory` owns a shared DRAM arena and one staging arena per sysnode. (code: software/kohakutpu/application/kohakutpu/application/runtime/memory/__init__.py:15-43; evidence: Code inspection; no qualifying card or named RTL witness established for this complete statement.)
- **L0.BIND.026** `unproven` A device allocation exposes at least one `line_bytes` word and otherwise rounds requested bytes up to that word size. Placement may reserve a larger alignment-rounded span; the MMU extent is the exposed extent, not the reserved span. (code: software/kohakutpu/application/kohakutpu/application/runtime/device/allocation.py:28-43; software/kohakutpu/application/kohakutpu/application/runtime/memory/arena.py:83-90; software/kohakuaccel/driver/kohakuaccel/driver/runtime/mmu.py:129-138; evidence: Code inspection; no qualifying card or named RTL witness established for this complete statement.)
- **L0.BIND.027** `unproven` Submissions retain named allocations through successful collection independently of owner free. Final queue completion retires its package tag; another tag or preparation pin retains storage. Eligible spilled packages may remain in the resident cache. Eviction invokes unspill for spilled allocations when that callback is supplied, or coalesces queue-local spans. (code: software/kohakutpu/application/kohakutpu/application/runtime/device/submission.py:443-482; software/kohakuaccel/driver/kohakuaccel/driver/node/queue/client.py:1126-1190; evidence: Code inspection; no qualifying card or named RTL witness established for this complete statement.)
- **L0.BIND.028** `unproven` Every allocation is tagged by its owner; `Arena.stats()` reports reserved and requested usage, peak reserved bytes, allocation count, free span count, largest free span, external fragmentation and owner totals. (tier-0 vector: L0.BIND.028.counters; mutant: memory/no-peak) (code: software/kohakutpu/application/kohakutpu/application/runtime/memory/arena.py:72-90, :152-174, :189-205)
- **L0.BIND.029** `unproven` With a paint callback, DRAM allocation zeroes from the painted high-water mark through the end of a newly reserved span. Reuse below that mark does not clear prior contents. Staging allocation does not paint; carving DRAM paints the carved reservation when a callback exists. (code: software/kohakutpu/application/kohakutpu/application/runtime/memory/__init__.py:28-35, :45-77, :130-139; evidence: Code inspection; no qualifying card or named RTL witness established for this complete statement.)
- **L0.BIND.030** `unproven` Recording uses the main heap and returns a Recorded log owning its allocation references. Closing releases references once; destruction closes it. TinyJit closes old capture logs before recapturing, retaining graph placement pins until graph cleanup. (code: software/kohakutpu/application/kohakutpu/application/runtime/device/replay.py:13-49; software/kohakutpu/application/kohakutpu/application/runtime/device/common.py:147-182; software/kohakutpu/application/kohakutpu/application/runtime/device/allocation.py:134-158; software/kohakutpu/application/kohakutpu/application/ktpugrad/device.py:421-436, :467-473; evidence: Code inspection; no qualifying card or named RTL witness established for this complete statement.)

## Host-only value templates

A `ktpu_l0::format::template::Template` owns an ordinary `Package` and a
`ParameterTable`. The table is host metadata serialized beside the package
bytes, not a sysnode table. Its `parameters` map names to inclusive `{lo, hi}`
class bounds. Each entry of `sites` names a payload index, destination bit,
width, source shift, constant, named coefficients and an optional buffer index.
Parameters, bounds, constants and coefficients are signed 64-bit integers;
evaluation uses checked signed 128-bit sums. Patched values are unsigned
64-bit integers. Buffer extents are the package buffer table's byte sizes;
bindings supply bases; optional `buffer_sizes` narrows launch extents without exceeding package extents.

- **L0.BIND.035** `unproven` `Template.bind(bindings, params)` evaluates each site's `constant + sum(coefficient[name] * params[name])`, adding the selected bound buffer base for an address site. The returned package has ordinary L0 bytes and no host metadata, buffers or relocations. (code: software/kohakuaccel/compiler/crates/ka-package/src/format/template.rs:252-272, :298-342; software/kohakuaccel/compiler/crates/ka-package/src/format/bind.rs:322-333; vector: L0.BIND.035.affine)
- **L0.BIND.036** `unproven` A value segment writes `((value >> shift) & (2^width - 1))` at `[bit, bit + width)` and preserves all other payload bits. Adjacent source segments with equal payload, constant, coefficients and optional buffer form a split value; only its top segment refuses high bits. Destination segments may be nonadjacent and may cross 64-bit limbs. (code: software/kohakuaccel/compiler/crates/ka-package/src/format/template.rs:274-295, :329-342; vector: L0.BIND.036.split)
- **L0.BIND.037** `unproven` Site validation rejects negative or out-of-u64 values, checked affine overflow and high bits beyond the top segment. Width is 1..64, shift is 0..63, shift plus width is at most 64, and a payload destination fits 256 bits. Invalid indices, empty site names and overlapping destinations, including ordinary relocations, refuse. Site errors identify the site and supplied parameters; environment, extent-override and ordinary-binding errors need not. The source template is unchanged. (code: software/kohakuaccel/compiler/crates/ka-package/src/format/template.rs:158-160,167-295,298-328)
- **L0.BIND.038** `unproven` Binding refuses missing or unknown parameters, undeclared coefficient names, inverted class bounds, and parameter values outside inclusive lo..hi. A class refusal names the affected site list and supplied parameter values. (code: software/kohakuaccel/compiler/crates/ka-package/src/format/template.rs:167-194, :252-260; vector: L0.BIND.038.class)
- **L0.BIND.039** `unproven` An address site requires its affine offset in `[0, effective_buffer.size)` before adding the bound base. `buffer_sizes` may supply one positive extent per buffer no larger than the package extent; these extents also govern translation. Invalid buffer indices and extents refuse. Site extent failures identify the site and parameters; invalid extent overrides report `L0.BIND.039` without site context. Address-space and field-width checks also apply. (code: software/kohakuaccel/compiler/crates/ka-package/src/format/template.rs:99-151, :263-294; evidence: Code inspection; no qualifying card or named RTL witness established for this complete statement.)
- **L0.BIND.040** `unproven` With no parameter declarations, sites, environment constraint or extent overrides, and no parameter values, template binding delegates to ordinary binding of a cloned package and returns the same result as `Package.bind(bindings)`. (code: software/kohakuaccel/compiler/crates/ka-package/src/format/template.rs:167-209, :298-328, :344-369; evidence: Code inspection; no qualifying card or named RTL witness established for this complete statement.)

Python `_rs.bind` accepts `translations` (driver virtual/physical/extent tuples),
`params` and `template_json` (the JSON parameter
table). With neither argument, it performs ordinary binding. The existing
`after` cut precedes binding; optional dispatch-engine lowering follows it.
No generator, L1, L2, firmware or sysnode format participates in value binding.

## Step-count templates

A value site with `step` selects a step instead of a payload. `payload` must
be zero (or omitted), `buffer` absent, and `shift` zero. Only whole value
fields are admitted: REPEAT's repeats at step bit 96, width 16; DISPATCH,
FETCH, AWAIT and MWAIT counts at bit 32, width 32; ALU and JUMP immediate
arguments at bit 64, width 64, with IMM set. Opcodes, flags, routes,
payload indices, increment counts and other topology remain fixed.

- **L0.BIND.041** `unproven` A step value site uses the same affine evaluation, class bounds, width checks and overlap checks as a payload site. It writes only an admitted value field, preserving all other step bits. A nonexistent step or any other field refuses. Step and payload destinations are separate namespaces. (code: software/kohakuaccel/compiler/crates/ka-package/src/format/template.rs:74-97, :223-250, :335-338; vector: L0.BIND.041.step)
- **L0.BIND.042** `unproven` A bound DISPATCH count cannot extend past the payload table, including overflow in first + count. A bound REPEAT count must be nonzero. Failure leaves the source template unchanged and names the site and supplied parameters. FETCH memory extents remain the caller's responsibility: its address is not a template buffer reference. (code: software/kohakuaccel/compiler/crates/ka-package/src/format/template.rs:350-363; vector: L0.BIND.042.extent)
- **L0.BIND.043** `unproven` With step value sites, Register binding prefixes ALU MOV-immediate to r15 and PATCH for each such site, targets the original step shifted by the complete prefix length, and clears each destination until its PATCH executes. Each step site adds two 16-byte steps and no payloads. LOAD, ALU, JUMP, PATCH or REG users refuse that transformation. With no step sites the ordinarily bound package returns before that refusal. Payload sites add no prefix. Step binding writes on the host without prefix steps; the transformed Register package remains unlowered because it contains PATCH. (code: software/kohakuaccel/compiler/crates/ka-package/src/format/template.rs:374-423; software/kohakuaccel/compiler/crates/ka-package/src/engine.rs:397-403)

- **L0.BIND.044** `unproven` ALU and JUMP immediate arguments admit whole 64-bit value sites only with IMM set. Operation, register operands, branch target and flags remain fixed. This supports sysnode-loop bounds and strides; register-operand forms refuse a site on their unused argument. (code: software/kohakuaccel/compiler/crates/ka-package/src/format/template.rs:74-79, :223-234; vector: L0.BIND.044.immediates)
- **L0.BIND.045** `unproven` Binding validates every REPEAT's nonzero repetition and payload counts, payload/increment-table extent, increment field geometry, nondecreasing payload indices, nonoverlapping fields and field wrap. If an increment overlaps a relocation, it must match that relocation's field exactly and its final shifted addend must stay within the bound buffer. (code: software/kohakuaccel/compiler/crates/ka-package/src/format/bind.rs:210-284; evidence: implementation inspection only; no card or named RTL witness established for this complete contract.)

Python `_rs.bind` exposes `count_path="step"` (default) or `"register"`.
These are host-known counts; loading a card-known count and branching over a
body remains an explicitly authored LOAD/ALU/JUMP/PATCH program, not a
conversion performed by this binder. No firmware or wire-format change is
required. Differential derivation fits admitted step values while retaining
exact equality for every other step field.

## Rulings

The disagreements found on this page are ruled in `../rulings.md`: RUL.022, RUL.023, RUL.039, RUL.040.
