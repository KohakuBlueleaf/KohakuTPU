"""KohakuTPU's compiler: L1 text to the bytes a card receives.

- `target`: the v9 machine, and what the language's passes ask of it
- `encode`: each unit's machine code; `isa`: their CU instructions as tables
- `emit`: L1 bodies and images to a `Program`
- `program`, `imem`: the vector cores' resident images
- `layout`: how an L1 type lies in memory
- `build`: kernel -> L1 text -> Program -> package
"""
