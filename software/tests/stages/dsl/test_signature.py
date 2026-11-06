"""Ports must support the recorder's keyword invocation."""

import pytest

from kohakutpu.language import dsl as kt
from kohakutpu.language.dsl.errors import DSLError


def test_positional_only_port_is_refused_at_construction():
    def body(x=kt.In(16), /):
        pass

    with pytest.raises(DSLError, match="positional-or-keyword"):
        kt.kernel(body)
