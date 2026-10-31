"""`.ktpu`: KohakuTPU's kernel text, at L1 -- one instruction stream per unit
and the node's steps, as the node runs them.

- `text`: the statement syntax, modules, read-time expansion
- `l1`: the vector core's math mnemonics and what each reads and writes
- `numerics`: the number formats (MXFP7, the lanes' E8M15)
- `examples`: hand-written L1 kernels
"""
