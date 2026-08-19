# Five generated tops have drifted from their generator

`ktpu_mesh_2x2.v`, `ktpu_mesh_2x2_1port.v`, `ktpu_mesh_2x4.v`, `ktpu_mesh_4x2.v`
and `ktpu_mesh_4x4.v` **cannot be reproduced by `gen_mesh.py`** and nothing
references them: no synth target in `tests/run_synth_check.ps1`, no bench in
`scripts/py/xsim.py`, no script. The only mention anywhere else is a provenance
string in `boards/singlemesh_2x2.json` naming which synthesis log some
coordinates were read from — a historical record, not a dependency.

They predate, at least:

- the `TILES` / `GA` / `GB` / `L1_PRIM` parameters, so they elaborate
  `mx_cluster_cu`'s defaults
- `INST_DEPTH` / `RECV_DEPTH` pass-through, so their FIFOs are 32/64 deep
- `FIFO_DEPTH(512)` on the routers
- the removal of the `obs` port, so their boundary is not clock/reset/AXI only

## Why this is a hazard and not just clutter

**Synthesising one produces a machine whose capacities do not match the
compiler**, silently. That exact mismatch — a bitstream at `TILES=256 GA=32
GB=32` against a planner assuming 512/128/256 — put 15,440 of 16,384 elements
past 10% error on real silicon while every gating check passed. A stale top is
the same fault waiting in a file that looks like a build target.

The three live tops **do** reproduce byte-exact from their maps
(`ktpu_ship_2x2`, `ktpu_ship_2x3`, `ktpu_ship_3x2`), so the generator is the
source of truth for everything anyone should build.

## What to do

Either delete them, or regenerate them from maps that do not currently exist
(there is no map for 4x4, 2x4, 4x2 or the 1-port variant). Deleting is the
honest option: a generated artefact whose generator can no longer produce it is
not a source file, and keeping it invites someone to synthesise it.

Not done here because removing sources is the repo owner's call.
