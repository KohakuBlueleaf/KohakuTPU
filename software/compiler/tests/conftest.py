"""Compiled kernels, shared across the compiler's tests: an L2 or L3 compile
of a whole kernel takes seconds, and several tests read the same one."""

import functools

import pytest
from kohakutpu.compiler import build
from kohakutpu.language import kernels


@functools.cache
def _l1_text(name: str, level: str) -> str:
    return build.l1_text(kernels.load(kernels.kernel(name)), name, level)


@pytest.fixture(scope="session")
def compiled():
    """``compiled(kernel, level)``: the kernel's L1 text from its `level` body."""
    return _l1_text
