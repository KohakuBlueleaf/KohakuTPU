"""The dense kernel lowers its complete key-domain reductions."""

import pytest

from kohakutpu.application.kernels.attention import kernel
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.language import dsl as kt
from kohakutpu.stack.kernel import specialise


@pytest.mark.parametrize("keys", [1024, 4096])
def test_dense_attention_lowers_the_complete_key_fold(keys):
    heads, queries, dim = 2, 256, 64
    given = {
        "q": kt.TensorSpec.make((heads * queries, dim)),
        "k": kt.TensorSpec.make((heads * keys, dim)),
        "vt": kt.TensorSpec.make((heads * dim, keys)),
        "valid": kt.TensorSpec.make((heads * queries, keys)),
    }
    compiled = specialise(
        kernel(heads, queries, keys, dim),
        given,
        machine=machine_of("multimesh_v9t8"),
    )
    assert not compiled.verdict["refused"], compiled.verdict
    assert compiled.packages
