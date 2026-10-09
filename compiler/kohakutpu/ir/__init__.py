"""The compiler's levels as data (docs/projects/kohakutpu/compiler.md s1).

`l3`: the tile program, `l2`: the schedule, `l1`: the instruction stream per
unit, typed; each with its text (`python -m kohakutpu.ir`). Each level lowers
only to the one below it, so a measured number belongs to exactly one level.
"""
