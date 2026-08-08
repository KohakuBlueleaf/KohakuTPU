"""Every example runs, and the ones that check themselves say OK.

An example that has rotted is worse than no example: it is the first thing a
newcomer runs, and a traceback there says the whole stack is broken. These
execute each file as a subprocess -- the way a reader would -- rather than
importing it, so a missing import or a top-level typo is caught too.
"""

import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
EXAMPLES = sorted((ROOT / "examples").glob("[0-9]*.py"))


def _run(path: pathlib.Path) -> str:
    env = {"PYTHONPATH": str(ROOT / "src"), "PATH": "", "SYSTEMROOT": ""}
    done = subprocess.run(
        [sys.executable, str(path)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={k: v for k, v in env.items() if v} | {"PYTHONPATH": str(ROOT / "src")},
        timeout=300,
        check=False,
    )
    assert done.returncode == 0, f"{path.name} exited {done.returncode}\n{done.stderr}"
    return done.stdout


def test_there_are_examples_to_run():
    assert len(EXAMPLES) >= 5


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_the_example_runs(path):
    _run(path)


@pytest.mark.parametrize(
    "path",
    [p for p in EXAMPLES if p.stem.startswith(("05", "06"))],
    ids=lambda p: p.stem,
)
def test_the_example_verifies_itself(path):
    """05 and 06 execute their kernel and compare against the reference. They
    print OK or FAIL, so a numeric regression shows up here and not only in the
    unit tests -- which is the point of an example that runs real numbers."""
    out = _run(path)
    assert "OK" in out
    assert "FAIL" not in out
