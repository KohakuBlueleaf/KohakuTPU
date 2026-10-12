"""Vector-core DSL examples: [R, W] rows in GT4 bands."""

from kohakutpu.language.examples.dsl.vector.activation import activation
from kohakutpu.language.examples.dsl.vector.glu import glu
from kohakutpu.language.examples.dsl.vector.norm import layernorm, rmsnorm
from kohakutpu.language.examples.dsl.vector.softmax import softmax

__all__ = ["activation", "glu", "layernorm", "rmsnorm", "softmax"]
