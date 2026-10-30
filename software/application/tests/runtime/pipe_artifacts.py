"""The pipe witnesses' records, exported once a session by `scripts/py/pipe`.

`pipe_cases/` holds what the exporters read of the capture for the cases the
tests run (a capture takes a full LeNet step)."""

import functools
import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "scripts/py/pipe"))

import export_chains
import export_epilogues
import export_folds
import export_general_witness
import export_predicate_witness

CASES = Path(__file__).resolve().parent / "pipe_cases"


def case(name):
    return json.loads((CASES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def pipe_library(tmp_path_factory):
    return tmp_path_factory.mktemp("pipe-library")


@pytest.fixture(scope="session")
def fold_record(pipe_library):
    @functools.cache
    def record(index):
        c = case(f"fold_{index}")
        return export_folds.record(
            c["kind"],
            c["ci"],
            c["converts"],
            c["consumer"],
            c["c"],
            c["layouts"],
            pipe_library,
        )

    return record


@pytest.fixture(scope="session")
def chain_record(pipe_library):
    c = case("chain_2632")
    return export_chains.record(
        c["query"], c["chain"], c["layouts"], c["q"], c["sources"], pipe_library
    )


@pytest.fixture(scope="session")
def epilogue_record(pipe_library):
    c = case("epilogue_2458")
    return export_epilogues.record(
        c["event"],
        c["metadata"],
        c["layout"],
        c["convert_key"],
        np.random.default_rng(124),
        pipe_library,
    )


@pytest.fixture(scope="session")
def general_records():
    return export_general_witness.records()


@pytest.fixture(scope="session")
def predicate_records():
    return export_predicate_witness.records()
