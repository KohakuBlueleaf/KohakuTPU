# Machine files

A machine file holds every hardware fact the software stack reads for one
image: unit kinds and their coordinates, each kind's profile, the network,
staging, mover, system node and firmware. The stack reads facts from it by
path (`vc.lanes`, `mg.tiles`, `fw.max_units`) and holds no hardware constant
of its own (RUL.001-RUL.018).

## Files

- `machines/<name>.json`, one per image, generated:
  - `multimesh_v9`, `multimesh_v9t4` ... `multimesh_v9t8`: the images built
    from `scripts/tcl/<ver>/00_config.tcl`;
  - `multimesh_v9t8-6c2v`: the v9t8 card with the 6 + 2 die
    (`ktpu_ship_2x2_6c2v_1m_nol2_pump`);
  - `sru_v9t7`: the v9t7 card with Streaming Row Units in place of the vector
    cores (`ktpu_ship_1x2_4c2s_1m_nol2_pump`, board `boards/sru_v9t7.json`);
  - `vlt_v9t6s_1n`: the one-compute-node Verilated model on its simulation die.
- `machines/measured.json`: the facts that come from a measurement, each with
  how it was measured. They are the only hand-written values.

A file holds `schema`, `name`, `image`, `machine` (the facts, nested by path)
and `sources` (each fact's path and where it came from: a repo `file:line`, a
board key, or a named derivation).

`schema` is the version of the schema definition the file was written for:
the first 16 hex digits of sha256 over ktpu-machine's `src/schema/*.rs` in
file-name order, `//` comments and all whitespace removed, one line per file.
A field added, removed, renamed or retyped changes it; a comment does not. The
generator writes it, `--check` fails on a file written for another version,
and both readers refuse such a file: Python (`kohakutpu.language.machine`)
against the tree's definition, Rust (`ktpu_machine::schema_version`) against
the definition it was built from, so a build older than the files says to
rebuild.

## The compile key

`Machine::digest` is sha256 of the canonical JSON of the facts a compile path
can read, and it keys everything compiled for a machine: the stage caches and
the graph library's `machine` directory (`_rs.library_machine`, with the stack
settings). A fact is keyed unless its schema field has the type `Runtime<T>`:
that field is out of the digest and out of `Machine::get`, and reading it takes
`.runtime()` (`Machine::runtime_fact` by path), which no compile path calls,
so rustc settles the read set. A
compile path covers the crates from graph to image plus the generators and
planners behind a library kernel; a Python generator's reads reach the key
through the graph it writes. A rebuild that moves a runtime fact (`fw.sections`,
the queue layout, wait codes) keeps every compiled artifact. `name`, `image`
and `sources` are not facts and are out of the digest.

`ktpu-machine/tests/key.rs` holds the set to the compile sources: every leaf of
every machine file moves the digest if and only if it is keyed, no compile
source reaches one (`.runtime()`, `runtime_fact`, the runtime's `Card::of`) and every keyed field is
named by a compile source. That last check is by field name, so a name another
struct also uses passes without a reader of this one.

A compile constant that a machine fact fixes reads that fact, keyed
(`fw.package.unbounded` through `Facts::unbounded`), or is refused against it
(`vc.crossbar_modes` against the encoder's `XM_NAMES`, ISA.VC.070). The stack's
image room `IMEM_ROOM` is its own choice under the keyed `vc.imem_words`;
`node.imem_bytes` is the node processor's instruction memory, which bounds the
firmware. Pairs where a keyed fact is derived from a runtime fact
(`mover.max_index`, `mg.dq_limit`, `mg.acc_bits`) and the bounds a compile
constant stays under hold on every machine file, by
`paired_facts_agree_on_every_machine` here and
`compile_constants_agree_with_every_machine` in `ktpu/tests/machine_key.rs`.

## Sections

| section | what it holds | source |
|---|---|---|
| `card` | nodes, chain, die module, clock profiles, DRAM, the host's address map | the board file (`scripts/py/gen_board.py`) |
| `die` | units by kind (`mg`, `vc`, `sr`, `simt`) with coordinates, system-node ports, fetch port, flit / data / address / coordinate widths, router depth, staging | board, die map, the die's generated top |
| `unit` | instruction and receive depths, the CU_DATA burst limit | the die's top, `vec_cu.v` |
| `mg` | count, L1 A / B entries and bank size, entry geometry and width, K step, accumulator tiles and mantissa bits, drain queue and burst, FILL count and peers, tile reuse distance, shared B limit | board, top, `mx_cluster_cu.v`, `mx_cluster_mgr_pump.v`, measured |
| `vc` | count, build, registers (vector, scalar, constant, predicate), marks, read copies, VL, lanes, chunks, significand bits, L1 words and banks, IMEM and RUN reach, queue depths, DUALQ, MX pack, crossbar and pack modes, descriptors and their dimensions, increment width, walk limit, fill costs | board, `VEC_CORE` defines over `v2_core.v`, `v2_agu.v`, `v2_lanes.v`, top, measured |
| `sr` | count, the SRU profile knobs, buffer and FIFO words, op store, finalise program, passes, row state, pool timing (null on a die without SR units) | board, top, `sru_cu.v`, `sru_core.v`, `sru_cfg.vh`, `ktpu-isa/src/unit/sru/config.rs` |
| `simt` | count | board |
| `mover` | index list, loop levels, burst, FIFO, configuration queue, fault code width, gather row limit, modes | `mm_mover.v`, `mx_tdesc.v` |
| `node` | processor memories and scratchpad base, uncached alias bit, control region, console FIFO, RX queue, dispatch engine depth and counters, completion queue, ring depth, mover register span and room writes | `rv64_syscore.v`, `rv64_noc_mbox.v`, `rv64_load_win.v`, `sysnode.v`, `dispatch_engine.v`, firmware headers |
| `il` | the interlink's signal slots and meshes | `mag_ilink.v` |
| `fw` | image, parts, unit classes, memory budget and use, unit / buffer limits, enumeration wait, region heaps, boot, queue and package formats (package copy limit), status, wait and signal codes | `software/firmware/build.py`, headers, `package/interp.c`, `build/fw/<image>.size` |

## DRAM write-line isolation

`card.dram.line_bytes` is the Xache/DRAM write beat in bytes, not the NoC
transfer word (`die.word_bytes`). The generator follows the image build's
`V8_STAGES` to `35_xache.tcl`, reads the selected Xache wrapper's `W`, and
records all three source locations. The shipped wrapper has `W=512`, so the
write line is 64 bytes while the NoC word is 32 bytes.

Compiler scratch arenas align every member and their reserved tail to this
line, including L2 temporaries lowered to L1 parameters. Each arena remains
one relocation buffer. Caller-provided tensor views retain their declared
layout; arena padding is between independent buffers, not tensor elements.

Tiled cluster-output members also begin on a complete cluster write burst:
`max(card.dram.line_bytes, mg.wburst * die.word_bytes)`. On the v9t6 machines
this is 256 bytes, matching the runtime arena's allocation alignment. A cluster
writes a burst as consecutive words; burst-aligned buffers keep those bursts
within AXI page boundaries. This alignment applies to the member's arena
offset even when preceding vector/mover preparation needs fewer bytes. The
serialized graph-library package and runtime share this arena layout; a model
comparison must preserve it rather than reassemble L1 with separately
allocated temporary buffers.

## DRAM capacity

`card.dram.bytes` is the usable flat DRAM capacity of the named machine.
For a card it is the board's `mem_words` times the 32-byte host word. For a
Verilated model it is the model board's `model.dram_words` times the 64-byte
channel word times the number of DRAM channels in the card build. The model
board also names its `model.bench`; `scripts/py/card_model.py --board` passes
that depth as `-G DRAM_WORDS` and refuses a second override. The model machine
`vlt_v9t6s_1n` describes `card_v9t6s_1n`.

There is no separate capacity in words in the machine file. `flat_gb` describes
the Xache address decoder's span, not the installed model's capacity; a model
can have smaller backing arrays without changing its address wiring.

The driver's `board_map` reads `card.dram.bytes`; its `size` is that capacity
and `scratch` / `arena_limit` reserve the last scratch megabyte. Offline and
attached runtimes use that map for their arenas. `Device.dram_bytes` reports
the physical capacity, not the arena's free bytes after reservations. A
Verilated runtime refuses a model whose public DRAM array sizes disagree
with the named machine. Bring-up uses the same map for both model and card.

## Unit-global addresses and staging

- **MACHINE.ADDR.001** `silicon` A unit-global staging address selects
  `die.address.special_bit`, clears `die.address.reserved_bit`, and encodes the
  destination mesh and aperture using `die.address.mesh_shift`, `mesh_bits`,
  `aperture_shift` and `aperture_bits`. (RTL: `mag_stage.v:85-93`; evidence:
  the silicon card runtime places each node queue in that mesh's staging
  aperture and submits its packages through the queue.)
- **MACHINE.STAGE.001** `silicon` Staging occupies `die.staging.aperture`,
  enabled in `die.staging.implemented`. Its byte capacity is
  `die.staging.entries * die.staging.entry_words * die.word_bytes`; banks
  interleave those entries. (RTL: `mag_stage.v:25-42,74-79,95-103`;
  evidence: each silicon card run's node queue sits in staging through
  `AP_STAGE`; the entry-word fixture independently distinguishes capacity
  from the bank count.)

`die.address` holds `special_bit`, `reserved_bit`, `mesh_shift`, `mesh_bits`,
`aperture_shift` and `aperture_bits`. The generator reads complete decode lines
in `src/kohakuaccel/sysnode/core/mag_stage.v`; each slice supplies both its
shift and width. Fields must be nonempty, disjoint and ordered from the special
bit down to the aperture, within `die.noc.addr_bits`.

`die.staging.entry_words`, `aperture` and `implemented` describe the staging
entry and aperture decode. Repeated entry widths and aperture constants in
`mag.v` and `mag_mem_port.v` must agree. Capacity in bytes is
`entries * entry_words * die.word_bytes`; `banks` interleaves the entries and
does not multiply capacity. Capacity must fit below the aperture selector.

`ktpu_machine::card::Card::of` constructs the runtime topology with optional
node-count and unit-enumeration overrides. It refuses a node count that does
not fit the mesh field. Its address methods refuse absent meshes, oversized
local bases and staging offsets outside the derived capacity. Python accesses
this view through `_rs.machine_view`, `_rs.global_addr` and `_rs.stage_addr`.

Fixture generation accepts repeated `--set PATH=JSON` options. Each path must
be an existing table row and its value must have the row's JSON type. The
fixture name includes the overrides and their provenance is `fixture: --set`.
The address layout is checked after the overrides. Address fixtures live in
`software/rust/crates/ktpu-machine/tests/fixtures`, separate from production
machine files, and tests compare regenerated contents byte for byte.

## Generating

```
cd scripts/py
python -m machine --all            # every image and variant
python -m machine --ver v9t6       # one image
python -m machine --all --check    # fail when a file differs from its sources
```

A fact is one row of `scripts/py/machine/table.py`: its path and the reader
that finds it (`read/image.py`: board, Tcl config, RTL parameter under the
image's defines, RTL signal width, array depth, instance override or decode
line, Verilog header define, firmware define or enum, firmware build,
measured). A new
fact is a new row; a new die shape is an entry in `VARIANTS`
(`scripts/py/machine/__main__.py`). The Python reader is
`kohakutpu.language.machine` (`load(name)`, `names()`); a missing fact raises.
