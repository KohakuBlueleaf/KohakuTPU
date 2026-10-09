"""The compiler's levels as data (docs/projects/kohakutpu/compiler.md s1).

`l1`: the instruction stream per unit, typed. Each level lowers only to the one
below it, so a measured number belongs to exactly one level.
"""
