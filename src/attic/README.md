# attic — deletion candidates, pending review

Nothing here is deleted until the user reviews this list. Each entry says why
it is believed dead; disagree by moving the file out.

| what | why it is here |
|---|---|
| `common/fp.v`, `common/lut.v`, `common/xorshift.v` | reached by no top; predate `mx_fpacc`/`kohaku_sdpram`/`mm_prng` |
| `legacy-axi/axi4_master.v`, `instruction_receiver.v` | pre-NoC control path, superseded by orchestrator dispatch |
| `sweeps/sb_p*f512.v` (10) | station port-count sweep wrappers; sweeps complete, results in docs |
| `sweeps/axi_n1_wrap_4/5.v` | axi_n1 OOC sweep wrappers, same status |
| `old-tops/ktpu_mesh_*.v` (5) | pre-`ktpu_ship` generated family; no bench, no BD references them |
| `legacy/alu_array.v`, `legacy/tc.v` | tensor-core era experiments |
| `STALE-TOPS.md` | its content is superseded by the generator manifest (`regen_tops.py`) |
