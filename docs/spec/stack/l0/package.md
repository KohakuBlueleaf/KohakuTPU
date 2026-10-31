# L0 package wire format

The bytes a compile stores and a node's dispatcher runs: a 96-byte header, then
the unit, buffer, step, relocation and payload tables. The writer is
`software/rust/crates/ktpu-l0/src/format/wire.rs` (`Package.to_bytes`,
`Package.from_bytes`); the reader on the node is
`software/firmware/kohakuaccel/package/interp.c` with the offsets in
`software/firmware/kohakuaccel/include/ka/package/format.h`. Step semantics are
[steps.md](steps.md); bindings and relocation are [binding.md](binding.md).

**What the card ran.** Every package of the 2026-10-21 v9t6 runs (node 0, one
node) was built by the package builder at commit `af7c1bb`:
`Program.build(fetch, residents)` with no address fields, then
`engine.lower`, then `Package.to_bytes`, submitted by `NodeQueue.run` with no
bindings. Those packages carried a unit table (MG and VC units, credit 512,
fetch port `(0, 2)`), steps and payloads; zero buffers, zero relocations,
signature 0, no checksum, `ack_reserve` 0, and only `ENGINE` steps (no card
kernel held a `goto` or a field patch, so no package kept a `JUMP` or a
`PATCH`). A clause about any other field value is RTL or unproven.

## Layout

- **L0.PKG.001** `silicon` A package is little-endian throughout. The header sits at offset 0; the unit, buffer, step, relocation and payload tables follow, each starting at an offset that is a multiple of 32 and padded with zero bytes to the next multiple of 32; the total length is a multiple of 32. The writer emits the tables in that order. (code: ktpu-l0/src/format/wire.rs; doc: docs/spec/package-format.md:34-46; evidence: every 2026-10-21 card package was written by `Package.to_bytes` and accepted by the firmware's layout check)
- **L0.PKG.002** `silicon` A reader takes each table's position from header words 5-9 and assumes no table order. (code: software/firmware/kohakuaccel/package/interp.c:1306-1310; ktpu-l0/src/format/wire.rs; doc: docs/spec/package-format.md:48-49; evidence: the firmware read every card package through these offsets)

## Header (96 bytes, twelve 64-bit words)

- **L0.PKG.003** `silicon` Word 0: `[31:0]` magic `0x4B50414B` ("KAPK"), `[47:32]` version `2`, `[63:48]` header bytes `96`. (code: ktpu-l0/src/format/wire.rs; software/firmware/kohakuaccel/include/ka/package/format.h:8-10,14; doc: docs/spec/package-format.md:55-57; evidence: every card package passed the firmware's magic and version check)
- **L0.PKG.004** `silicon` Word 1 is the machine signature; `0` means any machine and is accepted by every node without computing anything. (code: software/firmware/kohakuaccel/package/interp.c:1322-1325; ktpu-l0/src/format/wire.rs; doc: docs/spec/package-format.md:58,168; evidence: every card package carried signature 0 (`PackageBuilder` default, ktpu-l0/src/build/mod.rs) and ran)
- **L0.PKG.005** `unproven` A nonzero signature that differs from the node's own fails the package with `BAD_SIGNATURE` (0x12), detail 0, value the node's signature. (code: software/firmware/kohakuaccel/package/interp.c:1322-1325; doc: docs/spec/node-queue.md:155; evidence: no stack path writes a nonzero signature)
- **L0.PKG.006** `silicon` Word 2: `[31:0]` total bytes, `[63:32]` flags; flag bit 0 `CHECKSUM`, flag bit 1 `RX`. (code: ktpu-l0/src/format/wire.rs; software/firmware/kohakuaccel/include/ka/package/format.h:16,27-29; doc: docs/spec/package-format.md:59-60; evidence: every card package's total was read by the firmware's layout check; flags were 0)
- **L0.PKG.007** `RTL` The writer sets flag `RX` exactly when the package holds a `RECV` step. With `RX` set the firmware leaves flits in the node's RX queue for the package's `RECV`s. (code: ktpu-l0/src/format/wire.rs; software/firmware/kohakuaccel/package/interp.c:1299; doc: docs/spec/package-format.md:60; evidence: software/application/tests/test_l1_examples.py::test_push_and_recv and ::test_a_drain_into_the_node run RECV packages on the Verilated card)
- **L0.PKG.033** `unproven` With flag `RX` clear, every completion drain pops each RX flit and counts it as a stray. (code: software/firmware/kohakuaccel/package/interp.c:1299; software/firmware/kohakuaccel/dispatch/engine.c:121-126; doc: docs/spec/package-format.md:60; evidence: no card package or test receives a flit it does not `RECV`)
- **L0.PKG.008** `silicon` Word 3: `[31:0]` `n_payload`, `[63:32]` `n_reloc`. Word 4: `[15:0]` `n_buffer`, `[31:16]` `n_step`, `[47:32]` `n_unit`, `[63:48]` `ack_reserve`. (code: ktpu-l0/src/format/wire.rs; software/firmware/kohakuaccel/package/interp.c:1297-1305; doc: docs/spec/package-format.md:61-66; evidence: every card package's counts were read by the firmware)
- **L0.PKG.009** `silicon` Words 5-9 are the byte offsets of the unit, buffer, step, relocation and payload tables; word 10 is the checksum (0 when flag `CHECKSUM` is clear); word 11 is 0. (code: ktpu-l0/src/format/wire.rs; software/firmware/kohakuaccel/include/ka/package/format.h:19-24; doc: docs/spec/package-format.md:67-73; evidence: every card package)
- **L0.PKG.010** `unproven` The checksum is FNV-1a 64 (basis `0xCBF29CE484222325`, prime `0x100000001B3`) over bytes `[0, total)` with word 10 read as zero. The writer stores it when built with `checksum=True`; the firmware verifies it when flag `CHECKSUM` is set or the RUN entry's flag `CHECKSUM` is set, and a mismatch fails the package with `BAD_CHECKSUM` (0x13), value the computed sum. `Package.from_bytes` refuses a mismatch with `PackageError`. (code: ktpu-l0/src/format/wire.rs; software/firmware/kohakuaccel/package/interp.c:1326-1334; doc: docs/spec/package-format.md:72; evidence: no card or Verilator run set either flag)
- **L0.PKG.011** `unproven` The writer refuses (`PackageError`) a package with more than `0xFFFF` units, buffers or steps. (code: ktpu-l0/src/format/wire.rs)
- **L0.PKG.012** `unproven` The firmware fails a package whose word 0 `[31:0]` is not the magic with `BAD_MAGIC` (0x10), and one whose version or header size is not its own with `BAD_VERSION` (0x11); detail 0, value word 0 in both. `Package.from_bytes` refuses both. (code: software/firmware/kohakuaccel/package/interp.c:1290-1296; ktpu-l0/src/format/wire.rs; doc: docs/spec/package-format.md:75-76)
- **L0.PKG.013** `unproven` The firmware fails with `BAD_LAYOUT` (0x18), detail 0, value word 2, a package whose total is below 96, above the byte count the RUN entry names, or that has any table `[offset, offset + n × entry size)` running past the total. `Package.from_bytes` refuses data shorter than 96 bytes, a total past the data and a table past the total. (code: software/firmware/kohakuaccel/package/interp.c:1311-1318; ktpu-l0/src/format/wire.rs; doc: docs/spec/package-format.md:75-76)
- **L0.PKG.014** `unproven` The firmware fails with `TOO_LARGE` (0x15), detail `n_unit`, value `n_buffer`, a package with more than `fw.max_pkg_units` [32] units or more than `fw.max_buffers` [64] buffers. (code: software/firmware/kohakuaccel/package/interp.c:1319-1321; software/firmware/kohakuaccel/include/ka/dispatch/engine.h:12; software/firmware/kohakuaccel/include/ka/package/interp.h:7; doc: docs/spec/node-queue.md:158)
- **L0.PKG.015** `unproven` The firmware checks in this order and reports the first failure only: magic, version and header size, layout, size limits, signature, checksum. (code: software/firmware/kohakuaccel/package/interp.c:1288-1336; software/firmware/kohakuaccel/dispatch/engine.c:12-20)

## Unit table (16 bytes per unit)

- **L0.PKG.016** `silicon` Word 0 is the unit word `type << 32 | mesh << 16 | y << 8 | x` (type 16 bits, mesh, y and x 8 bits each); word 1 is `credit [31:0] | fetch [63:32]`. (code: ktpu-l0/src/format/wire.rs; software/firmware/kohakuaccel/dispatch/engine.c:48-51; software/firmware/kohakuaccel/package/interp.c:1508-1512; doc: docs/spec/package-format.md:80-87; evidence: every card package's unit table was read by the firmware and mapped into the dispatch engine)
- **L0.PKG.017** `silicon` The type is two ASCII characters, the first in `[47:40]`: `MG` is `0x4D47`, `VC` is `0x5643`. (code: ktpu-l0/src/format/mod.rs; doc: docs/spec/package-format.md:85; evidence: the card's MG and VC units)
- **L0.PKG.018** `silicon` A step names a unit by its index in the table. The builder adds a unit on first use, keyed by `(mesh, x, y)`. (code: ktpu-l0/src/build/mod.rs; doc: docs/spec/package-format.md:89-91; evidence: every card package)
- **L0.PKG.034** `unproven` The builder refuses (`PackageError`) a coordinate with no known type. (code: ktpu-l0/src/build/mod.rs)
- **L0.PKG.019** `silicon` `fetch` is `1 << 16 | y << 8 | x`, the memory port that streams the unit's words, or 0 for none. The builder gives every unit the one port the board names (`fetch_port`, `(0, 2)` on v9t6) or none. (code: ktpu-l0/src/build/mod.rs; software/application/kohakutpu/application/runtime/card.py:155; boards/multimesh_v9t6.json:70-73; doc: docs/spec/package-format.md:87; evidence: every card dispatch went out as a fetch request through that port)
- **L0.PKG.020** `silicon` `credit` bounds the unit's instructions in flight. `Program.build` writes the machine's `unit.inst_depth` [512] for every unit. (code: ktpu-l0/src/program.rs; ktpu-machine/src/card.rs (`unit.inst_depth`); doc: docs/spec/package-format.md:86; evidence: every card package carried credit 512)
- **L0.PKG.021** `silicon` The firmware's credit for a unit is the table's nonzero credit when the unit class has no credit bound and the node-wide cap is above it. (code: software/firmware/kohakuaccel/dispatch/engine.c:54-61; software/firmware/kohakuaccel/package/interp.c:1508-1512; doc: docs/spec/package-format.md:230-237; evidence: `ka_engine_add` ran for every unit of every card package (engine.c unchanged since af7c1bb): table credit 512, `MG` and `VC` with no class bound, cap `0xFFFFFFFF` under `cq_depth` 0)
- **L0.PKG.035** `unproven` A table credit of 0 takes the node-wide cap; a nonzero class bound below the credit lowers it; the node-wide cap lowers it; the result is at least 1. (code: software/firmware/kohakuaccel/dispatch/engine.c:54-61; doc: docs/spec/package-format.md:230-237; evidence: every card and test package carries credit 512 under the `0xFFFFFFFF` cap, and `MG` and `VC` have no class bound)
- **L0.PKG.022** `unproven` A unit whose mesh differs from the boot block's mesh is foreign: it stays in the table so its completions are attributed, and a `DISPATCH`, `REPEAT`, `FETCH` or `SEND` to it fails the package with `BAD_UNIT` (0x17), detail its index. (code: software/firmware/kohakuaccel/package/interp.c:1516-1518,1352-1355,1430-1433,1224-1226)
- **L0.PKG.023** `unproven` The firmware attributes a completion by its source coordinate; with two table entries at one coordinate, the first entry takes every completion from it. Coordinates are 4 bits each (`die.noc.pos_bits` [4]). (code: software/firmware/kohakuaccel/dispatch/engine.c:64-66,70-81; software/firmware/kohakuaccel/include/ka/hal/node.h:77-78)

## Buffer table (32 bytes per buffer)

- **L0.PKG.024** `unproven` Word 0 is FNV-1a 64 of the buffer's name (diagnostic; `from_bytes` names the buffer `#<hash>`), word 1 its size in bytes, word 2 its default unit-global address, word 3 `kind [7:0] | mesh [15:8]` with kinds 0 `INPUT`, 1 `OUTPUT`, 2 `INOUT`, 3 `TEMP`, 4 `CONST`. The firmware reads only word 2. (code: ktpu-l0/src/format/wire.rs; software/firmware/kohakuaccel/package/interp.c:1342; doc: docs/spec/package-format.md:93-100; evidence: no card or Verilator package reaches the firmware with a buffer; every current path binds on the host first)

## Step list (16 bytes per step)

- **L0.PKG.025** `silicon` Word 0 is `op [7:0] | flags [15:8] | unit [31:16] | count [63:32]`; word 1 is the 64-bit argument. The `unit` field holds a unit index, a mesh, a slot, a register or a step index by opcode ([steps.md](steps.md)). (code: ktpu-l0/src/format/wire.rs; software/firmware/kohakuaccel/package/interp.c:1349-1351,1424-1426; doc: docs/spec/package-format.md:102-110; evidence: every card package's `ENGINE` steps)

## Relocation table (16 bytes per entry)

- **L0.PKG.026** `unproven` Word 0 is `payload [31:0] | bit [39:32] | width [47:40] | shift [55:48] | buffer [63:56]`; word 1 is the addend, two's complement. The writer sorts the entries by `(payload, bit)`. (code: ktpu-l0/src/format/wire.rs; software/firmware/kohakuaccel/include/ka/package/format.h:35; doc: docs/spec/package-format.md:112-123; evidence: no package reaching a node carries a relocation)

## Payload table (32 bytes per word)

- **L0.PKG.027** `silicon` A payload is one 256-bit word, least significant 64 bits first: bytes 0-7 are `[63:0]` (the dispatch mailbox's `ARG0`) up to bytes 24-31, `[255:192]` (`ARG3`). No routing header is stored. (code: ktpu-l0/src/format/wire.rs; software/firmware/kohakuaccel/include/ka/hal/node.h:58-64; doc: docs/spec/package-format.md:125-131; evidence: every card payload (fetched instruction words and `ENGINE` entry words))

## Reading the package

- **L0.PKG.028** `silicon` The firmware reads the package at the unit-global address the RUN entry names. A package outside the cacheable range (some bit of `[39:28]` set, bits 39 and 38 clear), such as one in a node's staging store (bit 39 set), is read through the processor's uncached alias (bit `node.uncached_bit` [38]). (code: software/firmware/kohakuaccel/package/interp.c:51-60,89-93,1484; software/firmware/kohakuaccel/include/ka/hal/mem.h:8-17; evidence: every card package sat in the queue heap in node 0's staging store (card.py:211-213); LOG:89 `local 0` ([debug/firmware.md](../debug/firmware.md) `LOG`, the v9t6 card log))
- **L0.PKG.036** `unproven` A package in the cacheable range is read through the D-cache. (code: software/firmware/kohakuaccel/package/interp.c:51-60,89-93,1484-1489; evidence: no card or test package sits in the cacheable range)
- **L0.PKG.029** `unproven` A cached package has its D-cache invalidated before the first read; one of at most `fw.package.copy_bytes` bytes whose units do not fetch (no fetch port, a relocation present, or boot flag `NOFETCH`) is copied whole into the scratchpad and read from there. The size is the image's build knob `KA_PKG_COPY_BYTES` (software/firmware/build.py `DEFINES`): 0 in the dispatcher images, which compiles the copy out, so every package is read where it lies. (code: software/firmware/kohakuaccel/package/interp.c (`KA_PKG_COPY_BYTES`, `LOCAL`, `localise`); software/firmware/build.py (`DEFINES`); scripts/py/machine/table.py (`fw_package_copy_bytes`))

## Unit classes

- **L0.PKG.030** `silicon` The firmware looks each unit's type up in the image's class table: a name, an optional credit bound, a completion classifier and a fault printer. `MG` and `VC` are registered with no credit bound and the generic classifier. (code: software/firmware/kohakuaccel/dispatch/engine.c:52-53; software/firmware/kohakutpu/units/matmul.c:11-17; software/firmware/kohakutpu/units/vector.c:6-24; doc: docs/spec/package-format.md:416-423; evidence: the engine path, which every card package took, requires every unit to use the generic classifier (interp.c:906-910))
- **L0.PKG.037** `unproven` A type with no class gets the generic class: the generic classifier, no credit bound, no fault printer. (code: software/firmware/kohakuaccel/unit/registry.c:20-36; evidence: every card unit is `MG` or `VC`)

## Machine signature

- **L0.PKG.031** `unproven` The machine signature is FNV-1a 64 over the unit words, each as 8 little-endian bytes, sorted ascending. The firmware computes it once per boot over its boot unit table (given by the host or enumerated) and keeps it. (code: ktpu-l0/src/format/mod.rs; software/firmware/kohakuaccel/package/interp.c:104-130; doc: docs/spec/package-format.md:162-169; evidence: no package carries a nonzero signature)

## Writer checks

- **L0.PKG.032** `unproven` `Package.to_bytes` refuses (`PackageError`): a relocation outside 256 bits, with width 0 or above 64, shift above 63, buffer index above 255 or past the buffer table, or naming a payload past the table; a `DISPATCH` or `MOVER` reading payloads past the table, an `ENGINE` reading more than `ceil(count / 3)` payloads past it, a `REPEAT` whose template and increments pass it, a `STORE` or `CTRL` reading `count` payloads past it, a `LOAD` or `RECV` reading one, a `SEND` reading `count` (or with `MEM` 2) past it; a `DISPATCH`, `REPEAT`, `AWAIT`, `FETCH` or `SEND` naming a unit index past the table, a `RECV` naming one other than `0xFFFF`; a `FETCH` for a unit without a fetch port; a `JUMP` past the step count; a `LOAD`, `ALU` or `PATCH` register of 16 or more; a `PATCH` word index past its table. (code: ktpu-l0/src/format/wire.rs)

## Rulings

The disagreements found on this page are ruled in `../rulings.md`: RUL.004, RUL.005, RUL.034, RUL.041, RUL.100.
