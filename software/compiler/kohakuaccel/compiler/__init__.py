"""The KohakuAccel compiler backend: how to describe what a card receives.

- `isa`: declare an instruction layout once; get encode, decode, disassembly
- `machine`: a machine's meshes, units and address map
- `package`: the L0 package -- its wire format, a builder, the dispatch-engine
  lowering, the node mover's register writes
- `program`: an L1 program (per-unit streams, sync points) to a package
"""
