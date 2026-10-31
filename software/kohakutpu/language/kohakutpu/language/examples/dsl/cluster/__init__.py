"""Cluster DSL examples: MXFP7 contractions with vector-core epilogues.

Shared memory (the Xache): `linear`, `mlp`, `swiglu`, `attention` -- their
pars span every node's units and name no node. Cross-node connection:
`splitk` -- node n adds its accumulators into node 0's over the interlink.
"""

from kohakutpu.language.examples.dsl.cluster.attention import attention
from kohakutpu.language.examples.dsl.cluster.linear import linear
from kohakutpu.language.examples.dsl.cluster.mlp import mlp
from kohakutpu.language.examples.dsl.cluster.splitk import splitk
from kohakutpu.language.examples.dsl.cluster.swiglu import swiglu

__all__ = ["attention", "linear", "mlp", "splitk", "swiglu"]
