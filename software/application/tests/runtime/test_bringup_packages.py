"""Prepare bringup workloads offline through the runtime's Rust seam."""

import pytest

from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.application.runtime.packages import Prebuilt
from kohakutpu.application.tools.bringup import WORKLOADS, rung_setup


@pytest.mark.parametrize("name", sorted(WORKLOADS))
@pytest.mark.parametrize("node", [0, 1])
def test_workload_packages_are_prebuilt_for_the_requested_node(name, node):
    card = OfflineCard("multimesh_v9t8", nodes=[0, 1])
    result = rung_setup(card, name, 7, node=node)
    prog = result["prog"]
    if isinstance(prog, dict):
        assert set(prog) == {node}
        prog = prog[node]
    assert isinstance(prog, Prebuilt)
    assert prog.mesh == node
    assert prog.bound(False)
    card.release(result["held"])
