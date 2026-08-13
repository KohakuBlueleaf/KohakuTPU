"""The framework's central claim, as a test that fails when it stops being true.

``kohakuaccel`` must not import any project. Everything else in this file exists
to check that from a different angle, because the failure mode is gradual: one
convenience import of a project constant, and the framework quietly stops being
one.

Runs under pytest, or as ``python -m tests.test_isolation`` from ``driver/``.
"""

import importlib
import pkgutil
import subprocess
import sys

PROJECT_PREFIXES = ("ktpu", "kohakutpu", "examples")

# Run in a SUBPROCESS: once this process has imported the example, an in-process
# check would see it and blame the framework.
_PROBE = f"""
import importlib, pkgutil, sys
import kohakuaccel
names = ["kohakuaccel"] + [m.name for m in
         pkgutil.walk_packages(kohakuaccel.__path__, "kohakuaccel.")]
for n in names:
    importlib.import_module(n)
bad = sorted(m for m in sys.modules
             if any(m == p or m.startswith(p + ".") for p in {PROJECT_PREFIXES!r}))
print(len(names), ",".join(bad))
"""


def _framework_modules() -> list[str]:
    import kohakuaccel

    found = ["kohakuaccel"]
    for mod in pkgutil.walk_packages(kohakuaccel.__path__, "kohakuaccel."):
        found.append(mod.name)
    return found


def test_framework_imports_no_project() -> None:
    """Importing every framework module must pull in no project module."""
    done = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]),
    )
    assert done.returncode == 0, done.stderr
    count, _, leaked = done.stdout.strip().partition(" ")
    assert int(count) > 1, "walk_packages found no framework modules"
    assert not leaked, f"kohakuaccel pulled in project modules: {leaked}"


def test_every_framework_module_imports() -> None:
    """A package that cannot be imported cannot be depended on."""
    for name in _framework_modules():
        importlib.import_module(name)


def test_example_runs_on_the_framework_alone() -> None:
    """The example computes the right answer, which is the claim in full."""
    from examples.saxpy.sw.run import main

    assert main() == 0


def test_unknown_unit_type_still_reports() -> None:
    """An endpoint nobody registered is named, not crashed on."""
    from kohakuaccel.device import type_code
    from kohakuaccel.unit import UNKNOWN_DBG, UnitRegistry

    reg = UnitRegistry()
    assert reg.name_of(type_code("ZZ")) == "ZZ"
    assert reg.decode_dbg(0xDEAD_BEEF, type_code("ZZ")) == UNKNOWN_DBG


def test_target_rejects_a_misspelled_feature() -> None:
    """A typo must raise, not read as absent and select the slow path forever."""
    from dataclasses import dataclass
    from typing import ClassVar

    from kohakuaccel.machine import Target

    @dataclass(frozen=True)
    class M(Target):
        FEATURES: ClassVar[tuple[str, ...]] = ("fast_path",)

    assert M(features=frozenset({"fast_path"})).has("fast_path")
    assert not M().has("fast_path")

    for bad in ({"fastpath"}, {"fast_path", "nope"}):
        try:
            M(features=frozenset(bad))
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad} was accepted")


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
