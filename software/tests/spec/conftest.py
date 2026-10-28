"""Puts the spec harness and runners on the import path; puts the process
device back after every test (`harness.scope`)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.scope import scoped
from harness.simulators import prepare, release

#: Resumable, time-bounded pieces (off unless ``KTPU_SPEC_LEDGER`` is set).
pytest_plugins = ["harness.budget"]


@pytest.fixture(autouse=True)
def device_put_back(request):
    """Whatever a test attaches is gone after it, a runner's or not."""
    params = getattr(getattr(request.node, "callspec", None), "params", {})
    with scoped():
        prepare(params.get("v"), request.node.nodeid)
        yield


@pytest.fixture(scope="session", autouse=True)
def simulator_cleanup():
    yield
    release()
