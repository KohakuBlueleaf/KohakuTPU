"""Deletion boundary: no executable imports of the retired compiler or model."""

import ast
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# Each retired Python test and the Rust test functions that witness it.
WITNESSES = Path(__file__).parent / "fixtures/deletion_witnesses.json"


def retired(name):
    if name.startswith("kohakutpu.simulation.behave"):
        return True
    for prefix, kept in (
        ("kohakutpu.compiler.", "_rs"),
        ("kohakuaccel.compiler.", "isa"),
    ):
        if name.startswith(prefix):
            return name[len(prefix) :].split(".")[0] != kept
    return False


def test_no_remaining_import_uses_a_retired_module():
    bad = []
    for root in ("software", "scripts", "tests"):
        for path in (ROOT / root).rglob("*.py"):
            if any(
                part in ("build", "ref", ".venv") for part in path.parts
            ) or path.is_relative_to(ROOT / "software/rust/target"):
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
                names = []
                if isinstance(node, ast.Import):
                    names = [n.name for n in node.names]
                elif isinstance(node, ast.ImportFrom):
                    base = node.module or ""
                    names = [base] + [f"{base}.{n.name}" for n in node.names]
                elif (
                    isinstance(node, ast.Call)
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "import_module"
                ):
                    names = [node.args[0].value]
                if any(retired(n) for n in names):
                    bad.append(f"{path.relative_to(ROOT)}:{node.lineno}: {names}")
    assert not bad, "\n".join(bad)


@pytest.mark.parametrize(
    "source",
    [
        "from kohakutpu.compiler import kernel as K",
        "from kohakutpu.compiler import rs",
        "from kohakuaccel.compiler.package import format as F",
    ],
)
def test_import_guard_catches_t3_mutants(tmp_path, monkeypatch, source):
    root = tmp_path / "software"
    root.mkdir()
    (root / "mutant.py").write_text(source, encoding="utf-8")
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    with pytest.raises(AssertionError, match="mutant.py"):
        test_no_remaining_import_uses_a_retired_module()


def test_retired_package_bodies_are_absent():
    for rel, names in (
        ("software/compiler/kohakutpu/compiler", {"__init__.py"}),
        ("software/compiler/kohakuaccel/compiler", {"__init__.py", "isa.py"}),
        ("software/simulation/kohakutpu/simulation/behave", set()),
    ):
        assert {
            p.relative_to(ROOT / rel).as_posix() for p in (ROOT / rel).rglob("*.py")
        } == names
    assert not list((ROOT / "software/rust/golden").glob("*dump.py"))


def test_explicit_rust_witness_names_in_the_deletion_map_exist():
    """Existence only; crate test runs provide the independent execution gate."""
    rows = json.loads(WITNESSES.read_text(encoding="utf-8"))
    corpus = "\n".join(
        p.read_text(encoding="utf-8")
        for p in (ROOT / "software/rust/crates").rglob("*.rs")
    )
    missing = []
    count = 0
    for row in rows:
        for name in row["rust"]:
            count += 1
            if not re.search(rf"\bfn\s+{name}\b", corpus):
                missing.append(name)
    assert count > 100
    assert not missing, sorted(set(missing))
