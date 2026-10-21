"""Each test lowers afresh: `l2.vector` remembers its image choices by shape,
which a test patching the lowering's internals must not inherit."""

import pytest

from kohakutpu.compiler.l2 import vector


@pytest.fixture(autouse=True)
def _fresh_choices():
    vector._chosen.clear()
    yield
    vector._chosen.clear()
