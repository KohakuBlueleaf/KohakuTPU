"""Shared native finite class for one actual fused tinygrad scheduler AST."""

from unittest.mock import patch

import pytest
from examples.tinygrad import fusion_classes

from kohakutpu.application.runtime.class_library import ClassLibrary


def test_fused_ast_two_shapes_two_addresses_and_missing_class_mutation(tmp_path):
    root = tmp_path / "library"
    result = fusion_classes.measure(root, tmp_path / "record.json")
    assert result["certified_templates"] == 1
    assert result["admitted_elements"] == [2048, 4096]
    assert len({tuple(x) for x in result["addresses"]}) == 4
    assert result["class_hits"] == 2
    assert result["compile_calls"] == 0
    assert result["expressions"][0] == result["expressions"][1]
    with (
        patch.object(ClassLibrary, "lookup", return_value=None),
        pytest.raises(RuntimeError, match="library miss"),
    ):
        fusion_classes.measure(root, tmp_path / "mutant.json", library_only=True)


def test_class_value_verifier_mutations():
    expected = [[0.0, 1.0]] * 4
    assert fusion_classes.verify(expected, expected)["fp16_bit_identical"]
    for mutant in (
        expected[:3],
        [[0.0]] + expected[1:],
        [[-0.0, 1.0]] + expected[1:],
        [[0.0, 2.0]] + expected[1:],
        [[0.0, float("nan")]] + expected[1:],
    ):
        with pytest.raises(AssertionError):
            fusion_classes.verify(expected, mutant)
