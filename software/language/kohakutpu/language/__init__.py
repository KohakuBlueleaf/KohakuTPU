"""`.ktpu`: KohakuTPU's language, one IR with a body at L3, L2 or L1 per `fn`.

- `text`: the syntax every level shares, modules, read-time expansion
- `l3`, `l2`, `l1`: each level's nodes, reader, verifier and facts
- `lower`: L3 -> L2 planning, L2 -> L1 lowering
- `opt`: L2 -> L2 and L1 -> L1 passes
- `pipeline`: the stages in order, against a `target.Target`
- `numerics`: the number formats the types name
- `kernels`: the hand-written kernels
"""
