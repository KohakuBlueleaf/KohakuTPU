"""`mg` on a board with more rounds than the landing zone has destinations (ISSUES row
28): the four-die boards run 16 units twice, and a store past the mapped zone was the
user page fault of exit -271 (-(0x100 + 15))."""

import subprocess

import native
import pytest

APP = "os/apps/ktpu/mg.c"
MUTANT = ("(round_no % 12) * 0x400", "round_no * 0x400")


def run(exe, units):
    return subprocess.run(
        [str(exe), str(units)], capture_output=True, text=True, check=False
    )


@pytest.fixture(scope="module")
def exe(tmp_path_factory):
    return native.on_double(tmp_path_factory.mktemp("mg"), "mg_app", [], app=APP)


@pytest.mark.parametrize("units", [1, 4, 16])
def test_every_round_stays_mapped_and_checks_its_drain(exe, units):
    done = run(exe, units)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[-1] == "rc 0"
    assert f"mg: {units} MG units, {2 * units} rounds, ok" in done.stdout


def test_a_board_past_the_zone_dies_without_the_cycle(tmp_path):
    exe = native.on_double(tmp_path, "mg_app", [], MUTANT, APP)
    assert run(exe, 4).returncode == 0
    assert run(exe, 16).returncode < 0
