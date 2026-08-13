"""Compile with one half, execute with the other, check the arithmetic.

The two halves are never in one interpreter: the compiler emits its artifact as
JSON and the driver reads it. That is not a workaround for the two packages
sharing a name -- it is the independence claim made literal, and it only works
because an artifact is plain data with no register in it.

Run as ``python -m tests.test_end_to_end`` from ``compiler/``.
"""

import json
import subprocess
import sys
from pathlib import Path


def _roots() -> tuple[Path, Path]:
    compiler = Path(__file__).resolve().parents[1]
    return compiler, compiler.parent / "driver"


def compile_artifact() -> dict:
    """Run the compiler in its own interpreter and return its artifact."""
    compiler, _ = _roots()
    # `check=False`: the assertion below reports stderr, which
    # `CalledProcessError` would swallow.
    done = subprocess.run(
        [sys.executable, "-m", "examples.saxpy.main", "--json"],
        capture_output=True,
        text=True,
        cwd=str(compiler),
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def run_artifact(artifact: dict) -> dict:
    """Run the driver in its own interpreter against `artifact`."""
    _, driver = _roots()
    done = subprocess.run(
        [sys.executable, "-m", "examples.saxpy.sw.run_artifact"],
        input=json.dumps(artifact),
        capture_output=True,
        text=True,
        cwd=str(driver),
        check=False,
    )
    assert done.returncode == 0, done.stderr or done.stdout
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_artifact_is_plain_data() -> None:
    """It must survive a JSON round trip, or the halves cannot stay separate."""
    art = compile_artifact()
    assert json.loads(json.dumps(art)) == art
    assert art["flits"], "no payloads were staged"
    tags = {s[0] for s in art["steps"]}
    assert tags <= {"seed", "kick", "await", "barrier"}, f"unexpected steps {tags}"


def test_compiled_saxpy_computes_the_right_answer() -> None:
    """The whole claim, end to end and across two interpreters."""
    result = run_artifact(compile_artifact())
    assert result["code"] == 0, f"artifact halted with {result['code']}"
    assert not result["wrong"], f"{result['wrong']}/{result['n']} elements wrong"


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    bad = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as exc:
            bad += 1
            print(f"  FAIL  {t.__name__}: {exc}")
    print(f"  {len(tests) - bad}/{len(tests)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
