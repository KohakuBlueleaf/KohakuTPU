"""A pytest plugin: every test runs under mutant `KTPU_MUTANT`, on a spec build."""

import os

import pytest

from kohakutpu.stack import rs


@pytest.fixture(autouse=True)
def mutated():
    with rs.mutant(os.environ["KTPU_MUTANT"]):
        yield
