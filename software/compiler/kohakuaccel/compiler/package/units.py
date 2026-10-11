"""A machine's units as a package's unit table (package-format.md §2)."""

from kohakuaccel.compiler.package.format import type_code, unit_word


def machine_units(machine, mesh: int | None = None) -> list[int]:
    """Every unit on one mesh of `machine`, as package unit words."""
    here = machine.mesh(mesh)
    return [
        unit_word(type_code(kind), x, y, here.index)
        for kind, coords in sorted(here.units.items())
        for x, y in coords
    ]


def unit_types(machine, mesh: int | None = None) -> dict:
    """Coordinate -> unit type name, for one mesh."""
    here = machine.mesh(mesh)
    return {tuple(c): kind for kind, coords in here.units.items() for c in coords}
