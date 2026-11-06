"""Index admission and axis-sensitive operand rendering."""

import pytest

from kohakutpu.language.dsl.errors import DSLError
from kohakutpu.language.dsl.types import TensorSpec
from kohakutpu.language.dsl.values import Param, Value


@pytest.mark.parametrize("key", [(Ellipsis, 0, 0), (Ellipsis, Ellipsis)])
def test_param_refuses_ambiguous_axes(key):
    p = Param("p", TensorSpec((4,), "f16"))
    with pytest.raises(DSLError):
        p[key]


@pytest.mark.parametrize("key", [(slice(None), slice(None), 0), None])
def test_value_view_refuses_unrepresentable_axes(key):
    v = Value(None, "v", "f16", (4, 4), "reg")
    with pytest.raises(DSLError):
        v[key]


def test_equal_slices_keep_axis_specific_spelling():
    v = Value(None, "v", "f16", (4, 4), "reg")
    assert v[0:4, 0:4].text() == "v[0 : +4, 0:4]"
