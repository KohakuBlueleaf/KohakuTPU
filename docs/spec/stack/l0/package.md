---
title: L0 package wire format
summary: The bytes a compile stores and a sysnode's dispatcher runs.
tags:
  - spec
  - stack
  - l0
---
# L0 package wire format

The bytes a compile stores and a sysnode's dispatcher runs: a 96-byte header, then
the unit, buffer, step, relocation and payload tables. The writer is
`software/kohakuaccel/compiler/crates/ka-package/src/format/wire.rs` (`Package.to_bytes`,
`Package.from_bytes`); the reader on the sysnode is
`software/kohakuaccel/firmware/package/interp.c` with the offsets in
`software/kohakuaccel/firmware/include/ka/package/format.h`. Step semantics are
[steps.md](steps.md); bindings and relocation are [binding.md](binding.md).

**What the card ran.** Every package of the 2026-10-21 v9t6 runs (sysnode 0, one
sysnode) was built by the package builder at commit `af7c1bb`:
`Program.build(fetch, residents)` with no address fields, then
`engine.lower`, then `Package.to_bytes`, submitted by `NodeQueue.run` with no
bindings. Those packages carried a unit table (MG and VC units, credit 512,
fetch port `(0, 2)`), steps and payloads; zero buffers, zero relocations,
signature 0, no checksum, `ack_reserve` 0, and only `ENGINE` steps (no card
kernel held a `goto` or a field patch, so no package kept a `JUMP` or a
`PATCH`). A clause about any other field value is RTL or unproven.

## Layout

- **L0.PKG.001** `silicon` A package is little-endian throughout. The header sits at offset 0; the unit, buffer, step, relocation and payload tables follow, each starting at an offset that is a multiple of 32 and padded with zero bytes to the next multiple of 32; the total length is a multiple of 32. The writer emits the tables in that order. (code: software/kohakuaccel/compiler/crates/ka-package/src/format/wire.rs:33-37, :369-379, :403-408; doc: docs/spec/package-format.md:35-47; evidence: every 2026-10-21 card package was written by `Package.to_bytes` and accepted by the firmware's layout check; evidence: matching af7c1bb wire behavior at af7c1bb:software/compiler/kohakuaccel/compiler/package/format.py:267-345)
- **L0.PKG.002** `silicon` A reader takes each table's position from header words 5-9 and assumes no table order. (code: software/kohakuaccel/compiler/crates/ka-package/src/format/wire.rs:566-582; software/kohakuaccel/firmware/package/interp.c:1539-1543; doc: docs/spec/package-format.md:49-50; evidence: the firmware read every card package through these offsets; evidence: matching af7c1bb wire behavior at af7c1bb:software/compiler/kohakuaccel/compiler/package/format.py:419-433)

## Header (96 bytes, twelve 64-bit words)

- **L0.PKG.003** `silicon` Word 0: `[31:0]` magic `0x4B50414B` ("KAPK"), `[47:32]` version `2`, `[63:48]` header bytes `96`. (code: software/kohakuaccel/compiler/crates/ka-package/src/format/wire.rs:386-387; software/kohakuaccel/firmware/package/interp.c:1518-1523; doc: docs/spec/package-format.md:56-58; evidence: every card package passed the firmware's magic and version check; evidence: matching af7c1bb wire behavior at af7c1bb:software/compiler/kohakuaccel/compiler/package/format.py:324-337)
- **L0.PKG.004** `silicon` Word 1 is the machine signature; `0` means any machine and is accepted by every sysnode without computing a signature for this header check. (code: software/kohakuaccel/compiler/crates/ka-package/src/format/wire.rs:388; software/kohakuaccel/firmware/package/interp.c:1555-1557; doc: docs/spec/package-format.md:59,169; evidence: every card package carried signature 0 (`PackageBuilder` default, software/kohakuaccel/compiler/crates/ka-package/src/build/mod.rs) and ran; evidence: matching af7c1bb wire behavior at af7c1bb:software/compiler/kohakuaccel/compiler/package/format.py:324-337)
- **L0.PKG.005** `unproven` A nonzero package signature that differs from the machine signature fails BAD_SIGNATURE (0x12), detail 0 and value the computed machine signature. (code: software/kohakuaccel/firmware/package/interp.c:1555-1558; software/kohakuaccel/compiler/crates/ka-package/src/program.rs:497-501)
- **L0.PKG.006** `silicon` Word 2: `[31:0]` total bytes, `[63:32]` flags; flag bit 0 `CHECKSUM`, flag bit 1 `RX`. (code: software/kohakuaccel/compiler/crates/ka-package/src/format/wire.rs:381-384, :389; software/kohakuaccel/firmware/package/interp.c:1525-1527; doc: docs/spec/package-format.md:60-61; evidence: every card package's total was read by the firmware's layout check; flags were 0; evidence: matching af7c1bb wire behavior at af7c1bb:software/compiler/kohakuaccel/compiler/package/format.py:321-333)
- **L0.PKG.007** `unproven` The writer sets flag `RX` exactly when the package holds a `RECV` step. With `RX` set the firmware leaves flits in the sysnode's RX queue for the package's `RECV`s. (code: software/kohakuaccel/compiler/crates/ka-package/src/format/wire.rs:381-384; software/kohakuaccel/firmware/package/interp.c:1527; software/kohakuaccel/firmware/dispatch/engine.c:174-179; doc: docs/spec/package-format.md:61)
- **L0.PKG.033** `unproven` With flag `RX` clear, every completion drain pops each RX flit and counts it as a stray. (code: software/kohakuaccel/firmware/package/interp.c:1527; software/kohakuaccel/firmware/dispatch/engine.c:174-179; doc: docs/spec/package-format.md:61; evidence: no card package or test receives a flit it does not `RECV`)
- **L0.PKG.008** `silicon` Word 3: `[31:0]` `n_payload`, `[63:32]` `n_reloc`. Word 4: `[15:0]` `n_buffer`, `[31:16]` `n_step`, `[47:32]` `n_unit`, `[63:48]` `ack_reserve`. (code: software/kohakuaccel/compiler/crates/ka-package/src/format/wire.rs:390-394; software/kohakuaccel/firmware/package/interp.c:1528-1538; doc: docs/spec/package-format.md:62-67; evidence: every card package's counts were read by the firmware; evidence: matching af7c1bb wire behavior at af7c1bb:software/compiler/kohakuaccel/compiler/package/format.py:329-333)
- **L0.PKG.009** `silicon` Words 5-9 are the byte offsets of the unit, buffer, step, relocation and payload tables; word 10 is the checksum (0 when flag `CHECKSUM` is clear); word 11 is 0. (code: software/kohakuaccel/compiler/crates/ka-package/src/format/wire.rs:395-401, :409-413; doc: docs/spec/package-format.md:68-74; evidence: every card package; evidence: matching af7c1bb wire behavior at af7c1bb:software/compiler/kohakuaccel/compiler/package/format.py:324-337)
- **L0.PKG.010** `unproven` The host computes FNV-1a 64 over `[0,total)`, treating checksum word 10 as zero, and emits a checksum when requested. Firmware verifies when either package or RUN CHECKSUM is set, consuming whole eight-byte words while their starting offsets are below total; this equals the host byte range only for eight-byte-aligned totals. A mismatch returns BAD_CHECKSUM with the computed sum; the host parser also refuses a mismatch. (code: software/kohakuaccel/compiler/crates/ka-package/src/format/wire.rs:140-147, :409-413, :583-586; software/kohakuaccel/firmware/package/interp.c:1559-1567; evidence: Code inspection; no qualifying card or named RTL witness established for this complete statement.)
- **L0.PKG.011** `unproven` The writer refuses (`PackageError`) a package with more than `0xFFFF` units, buffers or steps. (code: software/kohakuaccel/compiler/crates/ka-package/src/format/wire.rs:156-177, :324)
- **L0.PKG.012** `unproven` The firmware fails a package whose word 0 `[31:0]` is not the magic with `BAD_MAGIC` (0x10), and one whose version or header size is not its own with `BAD_VERSION` (0x11); detail 0, value word 0 in both. `Package.from_bytes` refuses both. (code: software/kohakuaccel/compiler/crates/ka-package/src/format/wire.rs:548-557; software/kohakuaccel/firmware/package/interp.c:1518-1523; doc: docs/spec/package-format.md:76-77)
- **L0.PKG.013** `unproven` Firmware header admission rejects a declared total below the package header size, above the RUN length, or not eight-byte aligned. Each table must have offset at most total and count at most (total - offset) / entry_size. Failure returns BAD_LAYOUT with detail zero and value the size header word; the table checks avoid overflowing offset-plus-size arithmetic. (code: software/kohakuaccel/firmware/package/interp.c:1511-1514, :1544-1551; evidence: implementation inspection only; no card or named RTL witness established for this complete contract.)
- **L0.PKG.014** `unproven` The firmware fails with `TOO_LARGE` (0x15), detail `n_unit`, value `n_buffer`, a package with more than `fw.max_pkg_units` [32] units or more than `fw.max_buffers` [64] buffers. (code: software/kohakuaccel/firmware/package/interp.c:1552-1554; software/kohakuaccel/firmware/include/ka/dispatch/engine.h:12; software/kohakuaccel/firmware/include/ka/package/interp.h:7; doc: docs/spec/node-queue.md:193)
- **L0.PKG.015** `unproven` The firmware checks in this order and reports the first failure only: magic, version and header size, relocation count (any relocation fails `BAD_RELOC`, L0.BIND.017), layout, size limits, signature, checksum. (code: software/kohakuaccel/firmware/package/interp.c:1516-1569; software/kohakuaccel/firmware/dispatch/engine.c:48-62)

## Unit table (16 bytes per unit)

- **L0.PKG.016** `silicon` Word 0 is the unit word `type << 32 | mesh << 16 | y << 8 | x` (type 16 bits, mesh, y and x 8 bits each); word 1 is `credit [31:0] | fetch [63:32]`. (code: software/kohakuaccel/compiler/crates/ka-package/src/format/mod.rs:282, :355-358; software/kohakuaccel/compiler/crates/ka-package/src/format/wire.rs:325-331, :594-606; software/kohakuaccel/firmware/dispatch/engine.c:92-95; doc: docs/spec/package-format.md:81-88; evidence: every card package's unit table was read by the firmware and mapped into the dispatch engine; evidence: matching af7c1bb wire behavior at af7c1bb:software/compiler/kohakuaccel/compiler/package/format.py:270-278)
- **L0.PKG.017** `silicon` The type is two ASCII characters, the first in `[47:40]`: `MG` is `0x4D47`, `VC` is `0x5643`. (code: software/kohakuaccel/compiler/crates/ka-package/src/format/mod.rs:282, :285-298; doc: docs/spec/package-format.md:86; evidence: the card's MG and VC units; evidence: matching af7c1bb wire behavior at af7c1bb:software/compiler/kohakuaccel/compiler/package/format.py:164-169)
- **L0.PKG.018** `silicon` A step names a unit by its index in the table. The builder adds a unit on first use, keyed by `(mesh, x, y)`. (code: software/kohakuaccel/compiler/crates/ka-package/src/build/mod.rs:213-219, :234-243; doc: docs/spec/package-format.md:90-92; evidence: every card package)
- **L0.PKG.034** `unproven` For a new `(mesh,x,y)` coordinate, the builder refuses when neither a nonempty supplied type name nor its type map supplies a type. An already keyed coordinate returns its existing index without rechecking a new type. (code: software/kohakuaccel/compiler/crates/ka-package/src/build/mod.rs:213-226; evidence: Code inspection; no qualifying card or named RTL witness established for this complete statement.)
- **L0.PKG.019** `silicon` `fetch` is `1 << 16 | y << 8 | x`, the memory port that streams the unit's words, or 0 for none. The builder gives every unit the one port the board names (`fetch_port`, `(0, 2)` on v9t6) or none. (code: software/kohakuaccel/compiler/crates/ka-package/src/build/mod.rs:227-241; software/kohakutpu/application/kohakutpu/application/runtime/card/__init__.py:196; boards/multimesh_v9t6.json:77-80; doc: docs/spec/package-format.md:88; evidence: every card dispatch went out as a fetch request through that port)
- **L0.PKG.020** `silicon` `credit` bounds the unit's instructions in flight. `Program.build` writes the machine's `unit.inst_depth` [512] for every unit. (code: software/kohakuaccel/compiler/crates/ka-package/src/program.rs:494; doc: docs/spec/package-format.md:87; evidence: every card package carried credit 512)
- **L0.PKG.021** `silicon` The firmware's credit for a unit is the table's nonzero credit when the unit class has no credit bound and the sysnode-wide cap is above it. (code: software/kohakuaccel/firmware/dispatch/engine.c:98-105; software/kohakuaccel/firmware/package/interp.c:1818-1822; doc: docs/spec/package-format.md:231-238; evidence: `ka_engine_add` ran for every unit of every card package (af7c1bb:software/firmware/kohakuaccel/dispatch/engine.c:54-61; `credit ?` is now `credit != KA_PKG_CREDIT_UNBOUNDED ?` with the sentinel 0, the same test): table credit 512, `MG` and `VC` with no class bound, cap `0xFFFFFFFF` under `cq_depth` 0)
- **L0.PKG.035** `unproven` A table credit of 0 takes the sysnode-wide cap; a nonzero class bound below the credit lowers it; the sysnode-wide cap lowers it; the result is at least 1. (code: software/kohakuaccel/firmware/dispatch/engine.c:98-105; doc: docs/spec/package-format.md:231-238; evidence: every card and test package carries credit 512 under the `0xFFFFFFFF` cap, and `MG` and `VC` have no class bound)
- **L0.PKG.022** `unproven` A unit whose mesh differs from the boot block's mesh is foreign: it stays in the table so its completions are attributed, and a `DISPATCH`, `REPEAT`, `FETCH` or `SEND` to it fails the package with `BAD_UNIT` (0x17), detail its index. (code: software/kohakuaccel/firmware/package/interp.c:1826-1828, :1576-1579, :1667-1670, :1447-1449)
- **L0.PKG.023** `unproven` The firmware attributes a completion by its source coordinate; with two table entries at one coordinate, the first entry takes every completion from it. Coordinates are 4 bits each (`die.noc.pos_bits` [4]). (code: software/kohakuaccel/firmware/dispatch/engine.c:108-110, :114-125; software/kohakuaccel/firmware/include/ka/hal/control.h:84-85)

## Buffer table (32 bytes per buffer)

- **L0.PKG.024** `unproven` Buffer word 0 is FNV-1a 64 of the name; the host parser renders it as `#` followed by 16 lowercase hex digits. Word 1 is byte size, word 2 the default address, and word 3 is `kind [7:0] | mesh [15:8]`, with kinds INPUT=0, OUTPUT=1, INOUT=2, TEMP=3, CONST=4. The host parser rejects unknown kinds. Package rehoming checks buffer and unit meshes and rewrites the buffer mesh byte. Firmware does not interpret buffer entries: it uses their offset and count for layout and size checks. Checksum verification or an enabled whole-package copy can read their raw bytes. (code: software/kohakuaccel/compiler/crates/ka-package/src/format/wire.rs:332-343, :421-453, :607-621; software/kohakuaccel/firmware/package/interp.c:127-136, :1529-1553, :1559-1565; evidence: Code inspection; no qualifying card or named RTL witness established for this complete statement.)

## Step list (16 bytes per step)

- **L0.PKG.025** `silicon` Word 0 is `op [7:0] | flags [15:8] | unit [31:16] | count [63:32]`; word 1 is the 64-bit argument. The `unit` field holds a unit index, a mesh, a slot, a register or a step index by opcode ([steps.md](steps.md)). (code: software/kohakuaccel/compiler/crates/ka-package/src/format/wire.rs:344-356, :622-635; doc: docs/spec/package-format.md:103-111; evidence: every card package's `ENGINE` steps; evidence: matching af7c1bb wire behavior at af7c1bb:software/compiler/kohakuaccel/compiler/package/format.py:289-298)

## Relocation table (16 bytes per entry)

- **L0.PKG.026** `unproven` Word 0 is `payload [31:0] | bit [39:32] | width [47:40] | shift [55:48] | buffer [63:56]`; word 1 is the addend, two's complement. The writer sorts the entries by `(payload, bit)`. (code: software/kohakuaccel/compiler/crates/ka-package/src/format/wire.rs:357-368, :636-648; doc: docs/spec/package-format.md:113-124; evidence: no package reaching a sysnode carries a relocation)

## Payload table (32 bytes per word)

- **L0.PKG.027** `silicon` A payload is one 256-bit word, least significant 64 bits first: bytes 0-7 are `[63:0]` (the dispatch mailbox's `ARG0`) up to bytes 24-31, `[255:192]` (`ARG3`). No routing header is stored. (code: software/kohakuaccel/compiler/crates/ka-package/src/format/wire.rs:369-372, :649-654; software/kohakuaccel/firmware/include/ka/hal/control.h:65-71; doc: docs/spec/package-format.md:126-132; evidence: every card payload (fetched instruction words and `ENGINE` entry words); evidence: matching af7c1bb wire behavior at af7c1bb:software/compiler/kohakuaccel/compiler/package/format.py:312-314)

## Reading the package

- **L0.PKG.028** `silicon` The firmware reads the package at the RUN entry’s unit-global address. Cacheability requires all bits at and above bit 39 clear, at least one bit of `[38:28]` set, and the uncached-alias bit clear. Other addresses, including staging with bit 39 set, use `ka_ld64` and the processor’s uncached alias (`node.uncached_bit` [38]). (code: software/kohakuaccel/firmware/package/interp.c:101-110, :139-143, :1789-1794; software/kohakuaccel/firmware/include/ka/hal/mem.h:8-17; evidence: Card packages occupy staging; the unchanged uncached read path is also present in af7c1bb:software/firmware/kohakuaccel/package/interp.c:89-93, :1481-1488.)
- **L0.PKG.036** `unproven` A package in the cacheable range is read through the D-cache. (code: software/kohakuaccel/firmware/package/interp.c:101-110, :139-143, :1790-1794; evidence: no card or test package sits in the cacheable range)
- **L0.PKG.029** `unproven` Firmware invalidates D-cache before reading a cacheable package. After successful header validation it copies the package into scratchpad only when it is cacheable, KA_PKG_COPY_BYTES is nonzero, total fits that limit and no unit fetch is enabled; NOFETCH disables fetching for this decision. Production firmware rejects nonzero relocation counts before copying. Dispatcher image definitions set fw.package.copy_bytes [0], compiling out copying. (code: software/kohakuaccel/firmware/package/interp.c:114-136, :1529-1533, :1789-1794, :1811-1814; software/kohakutpu/firmware/build.py:125-139)

## Unit classes

- **L0.PKG.030** `silicon` The firmware looks each unit's type up in the image's class table: a name, an optional credit bound, a completion classifier and a fault printer. `MG` and `VC` are registered with no credit bound and the generic classifier. (code: software/kohakuaccel/firmware/dispatch/engine.c:96-97; software/kohakutpu/firmware/units/matmul.c:17-23; software/kohakutpu/firmware/units/vector.c:11-30; doc: docs/spec/package-format.md:417-424; evidence: the engine path, which every card package took, requires every unit to use the generic classifier (interp.c:1035-1037))
- **L0.PKG.037** `unproven` A type with no class gets the generic class: the generic classifier, no credit bound, no fault printer. (code: software/kohakuaccel/firmware/unit/registry.c:20-36; evidence: every card unit is `MG` or `VC`)

## Machine signature

- **L0.PKG.031** `unproven` The machine signature is FNV-1a 64 over ascending-sorted unit words, each represented as eight little-endian bytes. Firmware computes it from the boot unit table on first use and caches it. Normal program lowering supplies the machine signature. (code: software/kohakuaccel/compiler/crates/ka-package/src/format/mod.rs:316-321; software/kohakuaccel/compiler/crates/ka-package/src/program.rs:497-501; software/kohakuaccel/firmware/package/interp.c:145-179)

## Writer checks

- **L0.PKG.032** `unproven` `Package.to_bytes` calls its checker before emission. It checks 16-bit table counts and firmware unit/buffer limits, relocation payload/buffer references and field geometry, opcode-specific payload extents, unit references (RECV also permits `0xFFFF`), FETCH port presence and full-width host FETCH address-payload indices, JUMP targets, register bounds including ASSERT_ZERO, and PATCH target indices. Immediate LOAD requires exactly IMM and count zero; flagged AWAIT requires exactly IMM, count zero and argument zero; ASSERT_ZERO requires zero flags and argument. (code: software/kohakuaccel/compiler/crates/ka-package/src/format/wire.rs:155-320, :324; evidence: Code inspection; no qualifying card or named RTL witness established for this complete statement.)

## Rulings

The disagreements found on this page are ruled in `../rulings.md`: RUL.004, RUL.005, RUL.034, RUL.041, RUL.100.
