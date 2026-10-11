"""The component import rules, checked statically over every package module.

A module is named ``<side>.<component>.*``: the side is ``kohakuaccel`` (the
framework), ``kohakutpu`` or ``toyaccel`` (projects); the component is one of
COMPONENTS. Each module's own imports are checked, so the rules hold
transitively by induction:

* the framework imports no project, and a project imports no other project;
* a component imports only the components DEPENDS lists for it;
* simulation imports no cost model (COST_MODELS);
* imports sit at module level, never inside a function or class.
"""

import ast
import pathlib

SOFTWARE = pathlib.Path(__file__).resolve().parents[1]
MEMBERS = ("language", "compiler", "driver", "simulation", "application", "template")
SIDES = ("kohakuaccel", "kohakutpu", "toyaccel")
COMPONENTS = ("language", "compiler", "driver", "simulation", "application")

DEPENDS = {
    "language": {"language"},
    "compiler": {"compiler", "language"},
    "driver": {"driver"},
    "simulation": {"simulation", "driver", "compiler", "language"},
    "application": set(COMPONENTS),
}

#: Packages that estimate cost; a simulator must measure, never ask a model.
COST_MODELS = ("kohakutpu.language.opt",)


def owner(name: str) -> tuple[str, str | None] | None:
    """``(side, component)`` of a dotted module name, or None for a module
    outside the workspace. A bare namespace root has component None."""
    parts = name.split(".")
    if parts[0] not in SIDES:
        return None
    if len(parts) < 2 or parts[1] not in COMPONENTS:
        return parts[0], None
    return parts[0], parts[1]


def imported(module: str, is_package: bool, tree: ast.Module) -> list[tuple]:
    """Every ``(line, target, nested)`` import in `tree`, relative ones
    resolved against `module`; `nested` marks one inside a def or class."""
    out = []

    def visit(node, nested):
        for child in ast.iter_child_nodes(node):
            inner = nested or isinstance(
                child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
            )
            if isinstance(child, ast.Import):
                out.extend((child.lineno, a.name, nested) for a in child.names)
            elif isinstance(child, ast.ImportFrom):
                base = child.module or ""
                if child.level:
                    pkg = module.split(".")
                    if not is_package:
                        pkg = pkg[:-1]
                    pkg = pkg[: len(pkg) - (child.level - 1)]
                    base = ".".join(pkg + ([base] if base else []))
                for a in child.names:
                    # `from pkg import name` depends on `pkg`, except under a
                    # bare namespace root, where `name` is the component.
                    root = owner(base)
                    sub = f"{base}.{a.name}"
                    whole = root is not None and root[1] is None and owner(sub)
                    out.append((child.lineno, sub if whole else base, nested))
            visit(child, inner)

    visit(tree, False)
    return out


def violations(module: str, source: str, is_package: bool = False) -> list[str]:
    """What `module`'s imports break, one line per breach."""
    me = owner(module)
    bad = []
    for line, target, nested in imported(module, is_package, ast.parse(source)):
        where = f"{module}:{line} imports {target}"
        if nested:
            bad.append(f"{where} inside a def or class")
        them = owner(target)
        if me is None or them is None:
            continue
        (side, comp), (tside, tcomp) = me, them
        if tcomp is None:
            bad.append(f"{where}: a namespace root, not a component")
            continue
        if tside != side and tside != "kohakuaccel":
            bad.append(f"{where}: {side} may not import project {tside}")
        if side == "kohakuaccel" and tside != "kohakuaccel":
            bad.append(f"{where}: the framework imports no project")
        if tcomp not in DEPENDS[comp]:
            bad.append(f"{where}: {comp} may not import {tcomp}")
        if comp == "simulation" and target.startswith(COST_MODELS):
            bad.append(f"{where}: simulation imports no cost model")
    return bad


def modules() -> list[tuple[str, pathlib.Path]]:
    """Every package module in the workspace, as ``(dotted name, path)``."""
    out = []
    for member in MEMBERS:
        root = SOFTWARE / member
        for side in SIDES:
            for path in sorted((root / side).rglob("*.py")):
                rel = path.relative_to(root).with_suffix("")
                parts = list(rel.parts)
                if parts[-1] == "__init__":
                    parts.pop()
                out.append((".".join(parts), path))
    return out


def test_workspace_obeys_the_rules() -> None:
    found = modules()
    assert len(found) > 100, f"only {len(found)} modules found under {SOFTWARE}"
    bad = []
    for name, path in found:
        src = path.read_text(encoding="utf-8")
        bad += violations(name, src, path.name == "__init__.py")
    assert not bad, "\n".join(bad)


BREACHES = {
    "framework imports a project": (
        "kohakuaccel.driver.x",
        "from kohakutpu.driver.host import Card",
    ),
    "project imports another project": (
        "toyaccel.driver.x",
        "import kohakutpu.driver.units",
    ),
    "driver imports compiler": (
        "kohakuaccel.driver.x",
        "from kohakuaccel.compiler.package import format",
    ),
    "language imports compiler": (
        "kohakutpu.language.x",
        "from kohakutpu.compiler import target",
    ),
    "anything imports application": (
        "kohakutpu.simulation.x",
        "import kohakutpu.application.tools",
    ),
    "simulation imports a cost model": (
        "kohakutpu.simulation.x",
        "from kohakutpu.language.opt.schedule import cost",
    ),
    "relative import escapes the component": (
        "kohakuaccel.driver.x",
        "from ..compiler import machine",
    ),
    "namespace root": ("kohakutpu.driver.x", "import kohakuaccel"),
    "component through its namespace root": (
        "kohakutpu.language.x",
        "from kohakutpu import compiler",
    ),
    "import inside a function": (
        "kohakuaccel.driver.x",
        "def f():\n    import kohakuaccel.driver.device\n",
    ),
}

ALLOWED = {
    "project imports framework": (
        "kohakutpu.driver.x",
        "from kohakuaccel.driver.device import mover",
    ),
    "compiler imports language": (
        "kohakutpu.compiler.x",
        "from kohakutpu.language.l1 import ops",
    ),
    "simulation imports compiler and driver": (
        "kohakutpu.simulation.x",
        "import kohakuaccel.compiler.package\nimport kohakuaccel.driver.transport",
    ),
    "third-party and stdlib": ("kohakuaccel.driver.x", "import numpy\nimport os"),
    "relative import inside the component": (
        "kohakuaccel.driver.device.x",
        "from ..transport import base",
    ),
}


def test_checker_flags_every_breach() -> None:
    for what, (name, src) in BREACHES.items():
        assert violations(name, src), f"not flagged: {what}"


def test_checker_passes_what_is_allowed() -> None:
    for what, (name, src) in ALLOWED.items():
        assert not violations(name, src), f"flagged: {what}"
