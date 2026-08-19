# examples/saxpy — the second project, hardware half

The platform's acceptance test, mirroring the software side's
`test_example_runs_on_the_framework_alone`: a SAXPY compute unit built from
`templates/cu`, driven by the existing `driver/examples/saxpy` ISA (CU_TYPE
'SX' — the payload layout in `saxpy_cu.v` is field-for-field `sw/isa.py`).
KohakuTPU is deliberately NOT the example: it is too mature to show the
minimal path.

## What is here

| File | What it proves |
|---|---|
| `saxpy_cu.v` | The whole unit contract on `noc_cu_base`: the sw ISA decoded, plain reads + one burst write against the memory agent (memory-protocol.md §3.1/§4), tag-framed responses, the ack dropped, fault on a bad opcode, CU_DBG = cumulative elements per `sw/unit.py`. |
| `saxpy_cu_tb.v` | xsim bench `saxpy_cu`: unit-level, against a memory model — discovery, a partial-tail-line run (read-modify-write of the tail), n=0, fault, a full 8-beat batch run, CU_DBG. 20 checks + `kh_port_check` clean. |
| `saxpy_map.txt` | 1 router, 1 MAG port, two `sax` units. |
| `tokens_saxpy.py` | The project's generator vocabulary: `gen_mesh.py --tokens` maps `sax` to a `saxpy_cu` instance. This file is ALL a project supplies to compose its units into a mesh. |
| `generated/saxpy_mesh.v` | The generated mesh top — regenerate with `python scripts/py/gen_mesh.py src/examples/saxpy/saxpy_map.txt -m saxpy_mesh --tokens src/examples/saxpy/tokens_saxpy.py --single-master -o src/examples/saxpy/generated/saxpy_mesh.v`. |
| `saxpy_mesh_tb.v` | **The acceptance test**, xsim bench `saxpy_mesh`: the generated mesh driven the way a host drives the card — CAPS discovery, operand upload through S_AXI_MEM, the program staged and dispatched through the orchestrator (S_AXI_CTRL), SIG_DONE + NODE_STATUS observed, results read back bit-exact from both units. 14 checks. |

The datapath is exact on whole-valued float32 (|v| < 2^23) — bit-identical to
the software model on that domain, which is what the benches drive. A real
unit swaps `f2i`/MAC/`i2f` for an FMA and keeps every other line.

Two measured behaviors a new project should copy from the benches rather than
rediscover:

- An instruction's completion does NOT order against the DRAM write it caused
  (`memory-protocol.md` §7.2): the unit retires when its last `MEM_WR_DATA`
  is sent, and the port's write engine can issue AXI after the host's read of
  the same word. A host reading back immediately must poll (`mrd_expect`).
- `SIG_DONE` counts every signal including `SIG_BATCH_COMPLETE`; a one-
  instruction last-marked program is one count, not two.
