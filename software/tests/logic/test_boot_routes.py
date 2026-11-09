"""The boot table never declares a shared region the kernel's folded check misreads."""

import importlib
from pathlib import Path

import pytest


@pytest.fixture
def boot(monkeypatch):
    software = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(software / "kohakuaccel/os/tools"))
    return importlib.import_module("boot")


def test_a_shared_region_across_a_route_boundary_is_refused(boot):
    mask = 3 << boot.MESH_SHIFT
    edge = 1 << boot.MESH_SHIFT
    crossing = [(edge - 4096, 8192, boot.RG_DRAM_SHARED, 0)]
    with pytest.raises(ValueError, match="route boundary"):
        boot.shared_routes(crossing, mask)
    boot.shared_routes(crossing, 0)
    boot.shared_routes([(edge - 8192, 8192, boot.RG_DRAM_SHARED, 0)], mask)
    boot.shared_routes([(edge - 4096, 8192, boot.RG_DRAM_PRIV, 0)], mask)
