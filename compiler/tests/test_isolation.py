"""The compiler framework's boundary claims, as tests.

Runs under pytest, or as ``python -m tests.test_isolation`` from ``compiler/``.
"""

import importlib
import pkgutil
import subprocess
import sys
from pathlib import Path

# The driver is the one import that would quietly undo the split, so it is named
# here even though nothing should reach for it.
FORBIDDEN = ("kohakutpu", "examples", "ktpu")

_PROBE = f"""
import importlib, pkgutil, sys
import kohakuaccel
names = ["kohakuaccel"] + [m.name for m in
         pkgutil.walk_packages(kohakuaccel.__path__, "kohakuaccel.")]
for n in names:
    importlib.import_module(n)
bad = sorted(m for m in sys.modules
             if any(m == p or m.startswith(p + ".") for p in {FORBIDDEN!r}))
print(len(names), ",".join(bad))
"""


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_framework_imports_no_project() -> None:
    """Importing every framework module must pull in no project module."""
    # `check=False`: the assertion below reports stderr, which
    # `CalledProcessError` would swallow.
    done = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        cwd=str(_root()),
        check=False,
    )
    assert done.returncode == 0, done.stderr
    count, _, leaked = done.stdout.strip().partition(" ")
    assert int(count) > 5, f"walk_packages found only {count} modules"
    assert not leaked, f"kohakuaccel pulled in project modules: {leaked}"


def test_every_module_imports() -> None:
    import kohakuaccel

    for mod in pkgutil.walk_packages(kohakuaccel.__path__, "kohakuaccel."):
        importlib.import_module(mod.name)


def test_isa_rejects_a_value_that_does_not_fit() -> None:
    """The whole reason the toolkit exists: a field that silently truncates."""
    from kohakuaccel.backend import Field, InstFormat, ISAError, roundtrip

    fmt = InstFormat("t", [Field("op", 8, const=1), Field("n", 4)], width=16)
    roundtrip(fmt, n=15)
    for bad in (16, -1):
        try:
            fmt.encode(n=bad)
        except ISAError:
            pass
        else:
            raise AssertionError(f"{bad} was accepted into a 4-bit field")


def test_isa_rejects_an_overwide_layout() -> None:
    from kohakuaccel.backend import Field, InstFormat, ISAError

    try:
        InstFormat("t", [Field("a", 40)], width=32)
    except ISAError:
        return
    raise AssertionError("a 40-bit field fitted into 32 bits")


def test_isa_signed_roundtrip() -> None:
    from kohakuaccel.backend import Field, InstFormat, roundtrip

    fmt = InstFormat(
        "t", [Field("op", 8, const=1), Field("d", 8, signed=True)], width=16
    )
    for v in (-128, -1, 0, 127):
        roundtrip(fmt, d=v)


def test_dependency_cycle_is_reported() -> None:
    from kohakuaccel.ir import IRError, ScheduleIR

    s = ScheduleIR()
    a = s.task("SX", flits=1)
    b = s.task("SX", flits=1)
    s.depends(a, b)
    s.depends(b, a)
    try:
        s.verify()
    except IRError as exc:
        assert "cycle" in str(exc)
        return
    raise AssertionError("a cycle passed verification")


def test_pack_respects_the_staging_bound() -> None:
    """A round may not stage more flits than the staging RAM holds."""
    from kohakuaccel.backend import Backend
    from kohakuaccel.compile import compile
    from kohakuaccel.ir import ScheduleIR
    from kohakuaccel.machinespec import MachineSpec

    class B(Backend):
        def encode(self, task, ctx):
            return [0] * task.flits

    machine = MachineSpec(name="tiny", units={"U": ((1, 0),)}, stage_flits=4, ncmd=64)
    s = ScheduleIR()
    for _ in range(6):
        s.task("U", flits=2)
    compile(s, machine, B())
    assert len(s.rounds) == 3, "6 tasks of 2 flits into 4 flits wants 3 rounds"
    for rnd in s.rounds:
        assert sum(s[i].flits for i in rnd.tasks) <= machine.stage_flits


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
