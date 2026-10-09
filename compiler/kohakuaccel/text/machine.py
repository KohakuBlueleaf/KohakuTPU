"""The head every level's text opens with: ``level LN``, the machine and its
units by name.

    level l2
    machine "kohakutpu-l1"
    unit mg0 MG (1, 0)

A unit's name is its type, lower case, and its index among that type's
coordinates in order; a reader checks every `unit` line against the machine.
"""

from kohakuaccel.package.units import unit_types
from kohakuaccel.text.syntax import Name, Stmt, Str
from kohakuaccel.text.vocab import Reader, tup


def unit_names(machine) -> dict:
    """Coordinate -> unit name, for every unit of `machine`."""
    types = unit_types(machine)
    out = {}
    for kind in sorted(set(types.values())):
        for i, c in enumerate(sorted(c for c, k in types.items() if k == kind)):
            out[c] = f"{kind.lower()}{i}"
    return out


def header(level: str, machine) -> list:
    types = unit_types(machine)
    names = unit_names(machine)
    out = [Stmt("level", [Name(level)]), Stmt("machine", [Str(machine.name)])]
    out += [
        Stmt("unit", [Name(n), Name(types[c]), tup(*c)])
        for c, n in sorted(names.items(), key=lambda kv: kv[1])
    ]
    return out


class LevelReader(Reader):
    """Reading state with the head read: the machine and the units by name."""

    def __init__(self, source: str, file: str) -> None:
        super().__init__(source, file)
        self.machine = None
        self.types: dict = {}
        self.units: dict = {}

    def unit(self, st, t):
        n = self.name(st, t)
        if n not in self.units:
            self.fail(st, f"no unit {n!r} declared")
        return self.units[n]

    def level(self, stmts: list, level: str) -> list:
        """The statements after a ``level`` line naming `level`."""
        if not stmts or stmts[0].op != "level" or stmts[0].args != [Name(level)]:
            first = stmts[0] if stmts else Stmt("", line=1, col=1)
            self.fail(first, f"not `level {level}`")
        return stmts[1:]

    def head(self, st, machine=None, machines=None) -> bool:
        """Read `st` if it is a ``machine`` or ``unit`` line. The machine is
        `machine`, or the text's name looked up in `machines` (a dict)."""
        if st.op == "machine":
            (t,) = st.positional() or [None]
            if not isinstance(t, Str):
                self.fail(st, 'wanted `machine "NAME"`')
            if machine is not None and machine.name != t.text:
                self.fail(
                    st, f"the text is for {t.text!r}, the machine is {machine.name!r}"
                )
            if machine is None:
                if machines is None or t.text not in machines:
                    self.fail(st, f"no machine {t.text!r} given")
                machine = machines[t.text]
            self.machine = machine
            self.types = unit_types(machine)
            return True
        if st.op == "unit":
            if self.machine is None:
                self.fail(st, "a unit before its `machine`")
            n, kind, at = (st.positional() + [None, None, None])[:3]
            n, kind, coord = self.name(st, n), self.name(st, kind), self.ints(st, at)
            if self.types.get(coord) != kind:
                self.fail(st, f"{self.machine.name} has no {kind} at {coord}")
            if n in self.units:
                self.fail(st, f"unit {n!r} declared twice")
            self.units[n] = coord
            return True
        return False

    def need_machine(self, st) -> None:
        if self.machine is None:
            self.fail(st, f"a {st.op} before its `machine`")
