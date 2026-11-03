"""A product's N tile chosen by the plan's wave cost (`KTPU_MM_N_TILE=auto`,
`matmul::wave_cost`): ViT's QKV ([197, 192] x [576, 192]) takes the dense
tile, its FC1 ([197, 192] x [768, 192]) the plain one, on 2+2."""

import pytest

from kohakutpu.application.kernels import matmul as MM
from kohakutpu.stack import rs

MACHINE = rs.machine_json("multimesh_v9t6")


@pytest.fixture(autouse=True)
def plain_knobs(monkeypatch):
    for env, _ in MM.KNOBS.values():
        monkeypatch.delenv(env, raising=False)


def tn(n: int, tile: str) -> int:
    return MM.plan(1, 197, n, 192, machine_json=MACHINE, n_tile=tile).tn


@pytest.mark.parametrize(("n", "wanted"), [(576, "dense"), (768, "plain")])
def test_auto_takes_the_cheaper_n_tile(n, wanted):
    dense, plain = tn(n, "dense"), tn(n, "plain")
    assert dense != plain, (dense, plain)
    assert tn(n, "auto") == (dense if wanted == "dense" else plain)
    # Auto is the default.
    assert MM.plan(1, 197, n, 192, machine_json=MACHINE).tn == tn(n, "auto")
