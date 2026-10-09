"""L1 as text (docs/spec/ir-text.md §3): a file of L1 programs on one machine.

The framework reads and writes the program structure -- the head
(`kohakuaccel.text.machine`), `program` blocks of `send`, `mark`, `wait`,
`barrier`, `move` -- and nothing a unit is sent: those words are a project's,
registered per unit type on `L1Text.units`, for a move on `L1Text.mover`, and
for a top-level definition block (``image img0``) on `L1Text.defs`.
"""

from kohakuaccel.ir.l1.program import Program
from kohakuaccel.package.units import unit_types
from kohakuaccel.text.machine import LevelReader, header, unit_names
from kohakuaccel.text.syntax import Assign, Name, Stmt, emit, parse
from kohakuaccel.text.vocab import Vocabulary, hexa


class L1Writer:
    """Writing state: unit names, and the definitions ops ask for."""

    def __init__(self, machine) -> None:
        self.machine = machine
        self.unit_names = unit_names(machine)
        self.types = unit_types(machine)
        self.defs: list = []
        self._defined: dict = {}

    def define(self, key, prefix: str, make) -> str:
        """The name of the definition `key`, written once: ``make(name)``
        returns its statement."""
        if key not in self._defined:
            n = sum(1 for k in self._defined if k[0] == prefix)
            name = f"{prefix}{n}"
            self._defined[key] = name
            self.defs.append(make(name))
        return self._defined[key]


class L1Reader(LevelReader):
    """Reading state: the head, definitions, a program's mark tokens."""

    def __init__(self, source: str, file: str) -> None:
        super().__init__(source, file)
        self.tokens: dict = {}


class L1Text:
    """The L1 vocabularies of one project and the `Program` class it reads to."""

    def __init__(self, program=Program) -> None:
        self.program = program
        self.units: dict = {}
        self.mover = Vocabulary("l1 move")
        self.defs = Vocabulary("l1 definition")

    def unit(self, kind: str) -> Vocabulary:
        """The vocabulary of what a unit of type `kind` is sent."""
        return self.units.setdefault(kind, Vocabulary(f"l1 {kind} send"))

    # ----------------------------------------------------------------- write
    def write(self, programs, machine=None) -> str:
        """The text of `programs` (one `Program` or a list), on one machine."""
        programs = [programs] if isinstance(programs, Program) else list(programs)
        machine = machine if machine is not None else programs[0].machine
        w = L1Writer(machine)
        body = [
            Stmt("program", [Name(f"p{i}")], body=self._steps(w, p))
            for i, p in enumerate(programs)
        ]
        return emit(header("l1", machine) + w.defs + body)

    def _steps(self, w, prog) -> list:
        out = []
        for step in prog.steps:
            match step[0]:
                case "send":
                    _, coord, _, ops = step
                    voc = self.unit(w.types[coord])
                    lines = [s for op in ops for s in voc.write(w, op)]
                    out.append(Stmt("send", [Name(w.unit_names[coord])], body=lines))
                case "mark":
                    _, coord, token = step
                    out.append(
                        Stmt("mark", [Name(w.unit_names[coord])], target=f"t{token}")
                    )
                case "wait":
                    _, coord, token = step
                    args = [Name(w.unit_names[coord])]
                    if token is not None:
                        args += [Name("upto"), Name(f"t{token}")]
                    out.append(Stmt("wait", args))
                case "barrier":
                    out.append(Stmt("barrier"))
                case "move":
                    _, writes, ops = step
                    if ops:
                        lines = [s for op in ops for s in self.mover.write(w, op)]
                    else:
                        lines = [
                            Stmt("write", [Assign(hexa(r), hexa(v))]) for r, v in writes
                        ]
                    out.append(Stmt("move", body=lines))
        return out

    # ------------------------------------------------------------------ read
    def read(self, text: str, machine=None, machines=None, file="<l1>") -> list:
        """The programs of an L1 text; see `LevelReader.head` for the machine."""
        r = L1Reader(text, file)
        out = []
        for st in r.level(parse(text, file), "l1"):
            if r.head(st, machine, machines):
                continue
            if st.op == "program":
                r.need_machine(st)
                out.append(self._program(r, st))
                continue
            if st.target is not None or not st.positional():
                r.fail(st, f"no top-level statement {st.op!r}")
            key = r.name(st, st.positional()[0])
            if key in r.names:
                r.fail(st, f"{key!r} defined twice")
            r.names[key] = self.defs.read(r, st)
        return out

    def _program(self, r, st):
        prog = self.program(r.machine)
        r.tokens = {}
        for s in st.body:
            args = s.positional()
            match s.op:
                case "send":
                    coord = r.unit(s, args[0] if args else None)
                    voc = self.unit(r.types[coord])
                    prog.send(coord, *[self._op(r, b, voc, "flits") for b in s.body])
                case "mark":
                    if s.target is None or s.target in r.tokens:
                        r.fail(s, "a mark wants a new name: `tN = mark UNIT`")
                    r.tokens[s.target] = prog.mark(r.unit(s, args[0] if args else None))
                case "wait":
                    coord = r.unit(s, args[0] if args else None)
                    token = None
                    if len(args) == 3 and args[1] == Name("upto"):
                        t = r.name(s, args[2])
                        if t not in r.tokens:
                            r.fail(s, f"no mark {t!r} before this wait")
                        token = r.tokens[t]
                    elif len(args) != 1:
                        r.fail(s, "wanted `wait UNIT` or `wait UNIT upto MARK`")
                    try:
                        prog.wait(coord, token)
                    except ValueError as e:
                        r.fail(s, str(e))
                case "barrier":
                    prog.barrier()
                case "move":
                    if s.body and all(b.op == "write" for b in s.body):
                        prog.move([self._write(r, b) for b in s.body])
                    else:
                        prog.move(
                            [self._op(r, b, self.mover, "writes") for b in s.body]
                        )
                case _:
                    r.fail(s, f"no program statement {s.op!r}")
        return prog

    @staticmethod
    def _op(r, st, voc, lower: str):
        """One op, refused at its line if it does not lower."""
        op = voc.read(r, st)
        try:
            getattr(op, lower)()
        except ValueError as e:
            r.fail(st, str(e))
        return op

    @staticmethod
    def _write(r, st) -> tuple:
        (a,) = st.args or [None]
        if not isinstance(a, Assign):
            r.fail(st, "wanted `write REGISTER = VALUE`")
        return r.int(st, a.lhs), r.int(st, a.rhs)


__all__ = ["L1Reader", "L1Text", "L1Writer"]
