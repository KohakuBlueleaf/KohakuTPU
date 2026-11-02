"""`kdsmulti`'s chains on four homes (ISSUES row 26): every home's slots inside the
landing zone the kernel maps, the last word of each chain the one it computed. A slot
past the zone was the store fault that left the run with no result."""

import subprocess

import native
import pytest

APP = "os/apps/kds/graphs.c"
MUTANTS = {
    "homes_8k_apart": ("home * 0x1000UL", "home * 0x2000UL"),
    "chains_past_their_slots": (
        "if (k > 63) {\n        k = 63;",
        "if (k > 127) {\n        k = 127;",
    ),
}


def run(exe, nodes, steps):
    return subprocess.run(
        [str(exe), str(nodes), str(steps)], capture_output=True, text=True, check=False
    )


@pytest.fixture(scope="module")
def exe(tmp_path_factory):
    return native.on_double(tmp_path_factory.mktemp("kds"), "kds_app", [], app=APP)


@pytest.mark.parametrize(
    "nodes,steps", [(1, 8), (2, 8), (4, 1), (4, 8), (4, 63), (4, 200)]
)
def test_each_home_finishes_its_chain(exe, nodes, steps):
    done = run(exe, nodes, steps)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[-1] == "rc 0", done.stdout
    assert f"kdsmulti: {nodes} clients, ok" in done.stdout


@pytest.mark.parametrize("name", MUTANTS)
def test_each_mutant_dies_on_four_homes(tmp_path, name):
    exe = native.on_double(tmp_path, "kds_app", [], MUTANTS[name], APP)
    assert run(exe, 1, 8).returncode == 0
    assert run(exe, 4, 200).returncode < 0
