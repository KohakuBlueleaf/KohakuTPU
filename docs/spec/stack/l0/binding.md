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
and the bound-package path came after those runs; the Verilated-card tests in
`software/application/tests/test_l1_examples.py` run them. Firmware binding and
relocation run only for a package submitted with relocations, which no current
stack path sends.

## Absolute packages

- **L0.BIND.001** `silicon` A package with no buffers and no relocations runs at the absolute unit-global addresses its payloads carry; the runtime submits it with `nbind` 0 and bindings address 0. (code: software/application/kohakutpu/application/runtime/card.py:383-402; software/driver/kohakuaccel/driver/node/queue/client.py:269-285; software/firmware/kohakuaccel/package/interp.c:1338-1345; evidence: every 2026-10-21 card package)
- **L0.BIND.031** `unproven` Such a package runs the same whatever bindings its RUN entry names: with no buffer the firmware reads no binding. (code: software/firmware/kohakuaccel/package/interp.c:1338-1345; evidence: no card or Verilated run submits bindings)

## Making relocations (writer)

- **L0.BIND.002** `unproven` Given the project's address-field function `addresses(word, unit_type) -> ((segments, value), ...)` and live spans `{base: size}`, `PackageBuilder.payload` makes every reported field whose value lies in a span `[base, base + size)` a relocation: one entry per segment `(bit, width, shift)`, naming the span's buffer, with addend `value - base`, and clears the segment in the stored payload. A field whose value lies in no span keeps its absolute value. (code: ktpu-l0/src/build/mod.rs; ktpu-l0/src/build/fields.rs; doc: docs/spec/package-format.md:151-160; evidence: no test can witness it: the Verilated-card tests in software/application/tests/test_l1_examples.py bind each package at the recorded span bases before it is sent, so a field left absolute runs the same as one relocated; the offline witness (address-free gate keys equal across addresses, payloads equal a non-relocated build, an unbound mutant fails) ran on no card or Verilated model)
- **L0.BIND.003** `unproven` A span becomes a buffer the first time a field lands in it, in that order: name `buf<i>`, size the span's size, default address the span's base, kind `TEMP`, mesh the builder's mesh. (code: ktpu-l0/src/build/mod.rs; evidence: `Package.bind` drops the buffer table before any package is sent, so nothing on a card reads a buffer entry)
- **L0.BIND.004** `unproven` A split field (an address whose low and high bits sit apart, as the KohakuTPU encodings carry 40 bits as 34 + 6) is one relocation per segment, same buffer and addend, its own bit, width and shift. (code: ktpu-l0/src/build/mod.rs; ktpu-isa/src/reloc.rs; doc: docs/spec/package-format.md:146-149; evidence: as L0.BIND.002)
- **L0.BIND.005** `unproven` Once the smaller of 256 buffers (the relocation's 8-bit buffer field) and `fw.max_buffers` [64] exist, a field in a further span keeps its absolute value and makes no buffer. (code: ktpu-l0/src/build/mod.rs)
- **L0.BIND.006** `RTL` `bindings()` returns the recorded span bases in buffer order. (code: ktpu-l0/src/build/mod.rs; doc: docs/spec/package-format.md:445-450; evidence: software/application/tests/test_l1_examples.py, every test: each package is bound with these bindings before it runs (ktpu/src/python/mod.rs), and a binding out of order would move a relocated field of a multi-buffer example such as ::test_transpose into another buffer)
- **L0.BIND.032** `unproven` `build(defaults=False)` writes every buffer's default address as 0. (code: ktpu-l0/src/build/mod.rs; doc: docs/spec/package-format.md:445-450; evidence: every test binds every buffer to a nonzero address, so no default is read)
- **L0.BIND.007** `unproven` Two builds of one program whose buffers sit at different addresses produce byte-identical packages; only the bindings differ. (code: ktpu-l0/src/build/mod.rs; doc: docs/spec/package-format.md:151-155,445-449; evidence: witnessed offline only (address-free keys equal across addresses, payloads equal a non-relocated build), on no card or Verilated run)
- **L0.BIND.008** `unproven` `MOVER` payloads relocate through the address-field function like any unit word: the mover walker header's base and the index base are their address fields. `STORE`, `LOAD`, `RECV` and `SEND MEM` address words (a pair's address at `[63:0]` of its 128-bit half, a `LOAD`, `RECV` or push address at `[63:0]`) relocate when the builder method is called with `relocate=True`. `Program.build` enables the shared memory-field rule for every framework memory operation. Control offsets, register indices, values, descriptors and opaque flits are not inferred to be allocation addresses. (code: ktpu-l0/src/build/mod.rs; ktpu-l0/src/program.rs; ktpu-isa/src/reloc.rs; doc: docs/spec/package-format.md:349-355; evidence: as L0.BIND.002: a test cannot tell a relocated field from an absolute one)

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

- **L0.BIND.009** `RTL` A buffer's address is `bindings[i]` when `i < len(bindings)` and that binding is not the `UNBOUND` sentinel. (code: ktpu-l0/src/format/bind.rs; doc: docs/spec/package-format.md:135-136; evidence: software/application/tests/test_l1_examples.py, every test: each package is bound with nonzero bindings and its results checked on the Verilated card)
- **L0.BIND.033** `unproven` A missing binding or the `UNBOUND` sentinel takes the buffer's default address. Zero is a valid explicit address. (code: ktpu-l0/src/format/bind.rs; doc: docs/spec/package-format.md:135-136; evidence: every test binds every buffer, to a nonzero address)
- **L0.BIND.010** `RTL` A relocation sets payload bits `[bit, bit + width)` to `((address + addend) >> shift) & (2^width - 1)` and leaves every other bit of the payload as it was. (code: ktpu-l0/src/format/bind.rs; doc: docs/spec/package-format.md:141-144; evidence: software/application/tests/test_l1_examples.py, every test: the bound words are what the Verilated card runs, and a wrong field value misplaces a relocated address, the shifted high segment of a split field included)
- **L0.BIND.011** `RTL` `Package.bind(bindings)` returns the package with every relocation applied to its payloads and with no relocations. Its units and steps are unchanged except that host FETCH address indirection is resolved to an absolute argument and its host flag is cleared. (code: ktpu-l0/src/format/bind.rs; evidence: software/application/tests/test_l1_examples.py, every test: `engine.lower` refuses a package that still has relocations (ktpu-l0/src/engine.rs), and every test package lowers or runs as written)
- **L0.BIND.034** `unproven` The bound package has no buffers and no buffer names; its signature, `ack_reserve` and checksum flag are unchanged. (code: ktpu-l0/src/format/bind.rs; evidence: nothing on the card path reads them: every package carries signature 0, `ack_reserve` 0 and no checksum, and no relocation remains to name a buffer)
- **L0.BIND.012** `RTL` The runtime binds every package on the host before it is lowered and sent: `_rs.bind(lower=True)` returns the bound, lowered bytes, so the node receives no relocation and the RUN entry no bindings. (code: ktpu/src/python/mod.rs; software/application/kohakutpu/application/runtime/card.py:383-402; evidence: software/application/tests/test_l1_examples.py, every test)
- **L0.BIND.013** `unproven` `engine.lower` refuses (`PackageError`) a package that still has relocations: a package runs through the dispatch engine's entries only once bound. (code: ktpu-l0/src/engine.rs)
- **L0.BIND.014** `RTL` A package whose program jumps or patches is bound on the host like any other; `engine.lower` returns it unlowered and the node queues its steps itself ([dispatch-engine.md](dispatch-engine.md)). (code: ktpu-l0/src/engine.rs; ktpu/src/python/mod.rs; evidence: software/application/tests/test_l1_examples.py::test_loop_runs_a_count_from_memory, ::test_tiles_from_a_count_in_memory)

## Firmware binding boundary

The dispatcher refuses any nonzero relocation count with `BAD_RELOC`, detail
0 and value the count. It performs no binding or relocation. This RUL.022
boundary governs all firmware binding clauses below. Tier-1 vector: `RUL.022.card_refuses`; mutant image:
`kohakutpu_memory_mutant`. See [memory.md](memory.md).

- **L0.BIND.015** `unproven` The firmware does not read the RUN entry's binding array. The host resolves all bindings before submission. (code: software/firmware/kohakuaccel/package/interp.c; ruling: RUL.022)
- **L0.BIND.016** `unproven` Relocation arithmetic belongs exclusively to the host binder; no firmware execution path applies a relocation. (ruling: RUL.022)
- **L0.BIND.017** `unproven` A nonzero relocation count fails with `BAD_RELOC` (0x16), detail 0 and value the count, before any step executes. (code: software/firmware/kohakuaccel/package/interp.c `header`; tier-1 vector: RUL.022.card_refuses)
- **L0.BIND.018** `unproven` Firmware payload reads use host-bound words. REPEAT applies only its declared increments; PATCH applies only its declared updates. (code: software/firmware/kohakuaccel/package/interp.c)
- **L0.BIND.019** `unproven` The firmware rejects relocations regardless of their ordering; it holds no relocation cursor. (ruling: RUL.022)
- **L0.BIND.020** `unproven` A local package copy contains the same bound payloads as the submitted package. There is no firmware pre-relocation pass. (ruling: RUL.022)
- **L0.BIND.021** `unproven` A package with relocations reaches neither fetch nor mailbox dispatch: header validation refuses it. (tier-1 vector: RUL.022.card_refuses)

## Owner rulings on card memory (2026-10-21)

- **L0.BIND.022** `unproven` The card manages no memory while it has no MMU, hardware or software. All card memory management is the host driver's: a software card MMU in the driver. (doc: owner ruling 2026-10-21; evidence: CardMemory owns DRAM and staging; dispatcher images exclude region heaps; RUL.024.host_contract and RUL.024.card_refuses)
- **L0.BIND.023** `unproven` The compiler and the runtime name card memory by allocation handles, in card virtual addresses. The driver maps them to physical DRAM and rewrites every address field when it binds a package. (doc: owner ruling 2026-10-21; evidence: driver SoftwareMMU non-reused virtual ranges and generation checks; `_rs.bind` and `Template.bind_handles` resolve the live translation table; RUL.023.handles)
- **L0.BIND.024** `unproven` The driver's binding interface moves into KohakuAccel OS unchanged. KohakuAccel OS is a normal OS kernel plus SMP in software over the AMP hardware and a software MMU. (doc: owner ruling 2026-10-21)
- **L0.BIND.025** `unproven` The card has one flat DRAM arena, shared with the Xache, and a staging store per node. (doc: owner ruling 2026-10-21)
- **L0.BIND.026** `unproven` No allocation has a tail: every size is exact. (doc: owner ruling 2026-10-21)
- **L0.BIND.027** `unproven` Memory is freed by completion and dependency resolution. Device submissions retain every named allocation through successful collection, independently of owner free. Final queue completions retire package references; the last tag returns its package span. (tier-0 vector: L0.BIND.027.dependencies; mutant: memory/free-immediate; details: memory.md)
- **L0.BIND.028** `unproven` Every allocation is tagged by its owner; `Arena.stats()` reports reserved and requested usage, peak reserved bytes, allocation count, free span count, largest free span, external fragmentation and owner totals. (tier-0 vector: L0.BIND.028.counters; mutant: memory/no-peak)
- **L0.BIND.029** `unproven` Memory is zeroed on its first hand-out, by the allocator. (doc: owner ruling 2026-10-21)
- **L0.BIND.030** `unproven` TinyJit records on the main heap and pins the spans the recorded step used. The `Recorded` log owns its pins; replacing a capture releases the previous capture before recapturing. A changing-input TinyJit graph's numerical replay and pin ownership are tested on v9t6s in `test_memory_jit.py`. (tier-0 vector: L0.BIND.030.recording; mutant: memory/no-record-pins)

## Host-only value templates

A `ktpu_l0::format::template::Template` owns an ordinary `Package` and a
`ParameterTable`. The table is host metadata serialized beside the package
bytes, not a node table. Its `parameters` map names to inclusive `{lo, hi}`
class bounds. Each entry of `sites` names a payload index, destination bit,
width, source shift, constant, named coefficients and an optional buffer index.
Parameters, bounds, constants and coefficients are signed 64-bit integers;
evaluation uses checked signed 128-bit sums. Patched values are unsigned
64-bit integers. Buffer extents are the package buffer table's byte sizes;
bindings supply bases, not replacement sizes.

- **L0.BIND.035** `unproven` `Template.bind(bindings, params)` evaluates each site's `constant + sum(coefficient[name] * params[name])`, adding the selected bound buffer base for an address site. The returned package has ordinary L0 bytes and no host metadata, buffers or relocations. (code: software/rust/crates/ktpu-l0/src/format/template.rs; vector: L0.BIND.035.affine)
- **L0.BIND.036** `unproven` A value segment writes `((value >> shift) & (2^width - 1))` at `[bit, bit + width)` and preserves all other payload bits. Adjacent source segments with equal payload, constant, coefficients and optional buffer form a split value; only its top segment refuses high bits. Destination segments may be nonadjacent and may cross 64-bit limbs. (code: software/rust/crates/ktpu-l0/src/format/template.rs; vector: L0.BIND.036.split)
- **L0.BIND.037** `unproven` Binding refuses a negative value, a value outside unsigned 64-bit range, checked arithmetic overflow, or high bits beyond the top field segment after shifting. Each width is 1..64, shift is 0..63, shift + width is at most 64, and the destination fits its 256-bit payload. Invalid payload indices, empty site names and overlapping destination writes (including ordinary relocations) refuse. Errors identify the site and supplied parameter values; the source template is unchanged. (code: software/rust/crates/ktpu-l0/src/format/template.rs; vector: L0.BIND.037.width)
- **L0.BIND.038** `unproven` Binding refuses missing or unknown parameters, undeclared coefficient names, inverted class bounds, and parameter values outside inclusive lo..hi. A class refusal names the affected site list and supplied parameter values. (code: software/rust/crates/ktpu-l0/src/format/template.rs; vector: L0.BIND.038.class)
- **L0.BIND.039** `unproven` An address site's affine offset must lie in `[0, buffer.size)` before its bound base is added. An invalid buffer index refuses. An extent refusal names the site and supplied parameter values; field-width and unsigned address-space checks also apply to the resulting address. (code: software/rust/crates/ktpu-l0/src/format/template.rs; vector: L0.BIND.039.extent)
- **L0.BIND.040** `unproven` With no parameter declarations or sites and no parameter values, template binding produces exactly the bytes of ordinary `Package.bind(bindings)`, including its header, checksum, units, steps, payloads, and removal of buffer and relocation tables. (code: software/rust/crates/ktpu-l0/src/format/template.rs; vector: L0.BIND.040.identity)

Python `_rs.bind` accepts `translations` (driver virtual/physical/extent tuples),
`params` and `template_json` (the JSON parameter
table). With neither argument, it performs ordinary binding. The existing
`after` cut precedes binding; optional dispatch-engine lowering follows it.
No generator, L1, L2, firmware or node format participates in value binding.

## Step-count templates

A value site with `step` selects a step instead of a payload. `payload` must
be zero (or omitted), `buffer` absent, and `shift` zero. Only whole value
fields are admitted: REPEAT's repeats at step bit 96, width 16; DISPATCH,
FETCH, AWAIT and MWAIT counts at bit 32, width 32; ALU and JUMP immediate
arguments at bit 64, width 64, with IMM set. Opcodes, flags, routes,
payload indices, increment counts and other topology remain fixed.

- **L0.BIND.041** `unproven` A step value site uses the same affine evaluation, class bounds, width checks and overlap checks as a payload site. It writes only an admitted value field, preserving all other step bits. A nonexistent step or any other field refuses. Step and payload destinations are separate namespaces. (code: software/rust/crates/ktpu-l0/src/format/template.rs; vector: L0.BIND.041.step)
- **L0.BIND.042** `unproven` A bound DISPATCH count cannot extend past the payload table, including overflow in first + count. A bound REPEAT count must be nonzero. Failure leaves the source template unchanged and names the site and supplied parameters. FETCH memory extents remain the caller's responsibility: its address is not a template buffer reference. (code: software/rust/crates/ktpu-l0/src/format/template.rs; vector: L0.BIND.042.extent)
- **L0.BIND.043** `unproven` `bind_with_path(..., Register)` installs each bound step value using an ALU MOV immediate to r15 followed by PATCH to the original step's shifted index. It clears the destination value until that PATCH executes. Each site adds two 16-byte steps and no payloads; register/control-flow users (LOAD, ALU, JUMP, PATCH or REG flags) refuse. The Step path writes on the host and adds no steps. The register path contains PATCH and remains unlowered under L0.BIND.014 and L0.STEP.059. (code: software/rust/crates/ktpu-l0/src/format/template.rs; vector: L0.BIND.043.register)

- **L0.BIND.044** `unproven` ALU and JUMP immediate arguments admit whole 64-bit value sites only with IMM set. Operation, register operands, branch target and flags remain fixed. This supports node-loop bounds and strides; register-operand forms refuse a site on their unused argument. (code: software/rust/crates/ktpu-l0/src/format/template.rs; vector: L0.BIND.044.immediates)
- **L0.BIND.045** `unproven` After populating the final repeat counts and payload values (including parameterized buffer addresses), and before discarding relocation metadata, the binder checks that each increment cannot wrap its selected field and that the final incremented relocation addend stays within the buffer. A split low address field may not carry into its separately encoded high field; such a binding refuses rather than losing the carry. (code: software/rust/crates/ktpu-l0/src/format/bind.rs; host tests: relocated_repeat_checks_final_address_and_split_field_carry, template_repeat_checks_final_count_and_parameter_address; vectors: L0.BIND.045.repeat-carry, L0.BIND.045.bound-repeat-count)

Python `_rs.bind` exposes `count_path="step"` (default) or `"register"`.
These are host-known counts; loading a card-known count and branching over a
body remains an explicitly authored LOAD/ALU/JUMP/PATCH program, not a
conversion performed by this binder. No firmware or wire-format change is
required. Differential derivation fits admitted step values while retaining
exact equality for every other step field.

## Rulings

The disagreements found on this page are ruled in `../rulings.md`: RUL.022, RUL.023, RUL.039, RUL.040.
