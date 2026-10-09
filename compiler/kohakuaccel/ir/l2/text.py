"""L2 as text (docs/spec/ir-text.md §4): one schedule on one machine.

After the head (`kohakuaccel.text.machine`), a ``buffer`` line a buffer --
its layout a call of the layout's fields, its address after ``at``, a unit's
local storage as ``local=UNIT`` -- then the items in sequence order, inside
``package N`` blocks: ``item KIND on UNIT`` (a unit name when placed, a unit
type when not, ``mover``) with one ``with`` line of parameters and a ``read``
and a ``write`` line of views (``buf`` whole, ``buf[lo : hi]`` bytes).

A parameter that is an address inside a memory buffer prints as ``buf+off``.
The framework knows no layout and no kind: a project registers its layouts
(frozen dataclasses) with `L2Text.layout` and its kinds with `L2Text.kind`.
"""

import bisect
import dataclasses
import math
import re

from kohakuaccel.ir.l2.schedule import Schedule, View
from kohakuaccel.text.machine import LevelReader, header, unit_names
from kohakuaccel.text.syntax import (
    Assign,
    Call,
    Float,
    Int,
    Kw,
    Name,
    Neg,
    Offset,
    Slice,
    Stmt,
    Str,
    Tuple,
    emit,
    parse,
)
from kohakuaccel.text.syntax import View as ViewTerm
from kohakuaccel.text.vocab import dec, hexa, num, size

#: Names a buffer may not take: they are values.
WORDS = {"true": True, "false": False, "none": None}


@dataclasses.dataclass(frozen=True)
class Kind:
    """An item kind: its unit type and the parameters it takes."""

    unit: str
    required: tuple = ()
    optional: tuple = ()


class _Addresses:
    """Memory buffers by base, to name an address ``buf+off``."""

    def __init__(self, buffers, names) -> None:
        mem = sorted(
            (b.base, b.base + b.nbytes, names[id(b)])
            for b in buffers
            if b.space == "mem" and b.base is not None and b.nbytes
        )
        self.starts = [m[0] for m in mem]
        self.mem = mem

    def name(self, v: int):
        k = bisect.bisect_right(self.starts, v) - 1
        if k >= 0 and v < self.mem[k][1]:
            base, _, n = self.mem[k]
            return Name(n) if v == base else Offset(Name(n), num(v - base))
        return None


class L2Text:
    """The L2 layouts and item kinds of one project."""

    def __init__(self) -> None:
        self.layouts: dict = {}
        self.kinds: dict = {}

    def layout(self, cls):
        """Register a layout class (a frozen dataclass); returns it."""
        self.layouts[cls.__name__] = cls
        return cls

    def kind(self, name: str, unit: str, required=(), optional=()) -> None:
        self.kinds[name] = Kind(unit, tuple(required), tuple(optional))

    # ----------------------------------------------------------------- write
    def write(self, schedule: Schedule, machine=None) -> str:
        machine = machine if machine is not None else schedule.machine
        if machine is None:
            raise ValueError("an L2 text names its machine: pass one")
        names = self._names(schedule)
        units = unit_names(machine)
        addrs = _Addresses(schedule.buffers, names)
        out = header("l2", machine)
        out += [self._buffer(b, names, units) for b in schedule.buffers]
        block = None
        for it in schedule.items:
            if block is None or block.args != [dec(it.package)]:
                block = Stmt("package", [dec(it.package)])
                out.append(block)
            block.body.append(self._item(it, names, units, addrs))
        return emit(out)

    @staticmethod
    def _names(schedule) -> dict:
        """id(buffer) -> a name the grammar takes, unique."""
        out, taken = {}, set()
        for b in schedule.buffers:
            base = re.sub(r"\W", "_", b.name).strip("_") or "buf"
            if not re.match(r"[A-Za-z_]", base) or base in WORDS or base == "in":
                base = f"b_{base}"
            n, k = base, 1
            while n in taken:
                k += 1
                n = f"{base}_{k}"
            taken.add(n)
            out[id(b)] = n
        return out

    def _buffer(self, b, names, units) -> Stmt:
        args = [Name(names[id(b)])]
        if b.layout is not None:
            cls = type(b.layout)
            if self.layouts.get(cls.__name__) is not cls:
                raise TypeError(f"layout {cls.__name__} is not registered")
            fields = [
                Kw(f.name, _literal(getattr(b.layout, f.name)))
                for f in dataclasses.fields(b.layout)
            ]
            args.append(Call(cls.__name__, tuple(fields)))
        if b.base is not None:
            args += [Name("at"), hexa(b.base)]
        if b.local is not None:
            args.append(_kw("local", Name(units[tuple(b.local)])))
        if b.layout is None or getattr(b.layout, "nbytes", None) != b.nbytes:
            args.append(_kw("bytes", size(b.nbytes)))
        return Stmt("buffer", args)

    def _item(self, it, names, units, addrs) -> Stmt:
        kind = self.kinds.get(it.kind)
        if kind is None or kind.unit != it.unit:
            raise TypeError(f"item kind {it.kind!r} on {it.unit} is not registered")
        on = it.unit if it.at is None else units[tuple(it.at)]
        body = []
        if it.params:
            body.append(
                Stmt("with", [_kw(k, _value(v, addrs)) for k, v in it.params.items()])
            )
        for role, views in (("read", it.reads), ("write", it.writes)):
            if views:
                body.append(Stmt(role, [_view(v, names) for v in views], commas=True))
        return Stmt("item", [Name(it.kind), Name("on"), Name(on)], body=body)

    # ------------------------------------------------------------------ read
    def read(self, text: str, machine=None, machines=None, file="<l2>") -> Schedule:
        """The schedule of an L2 text; see `LevelReader.head` for the machine."""
        r = LevelReader(text, file)
        s = Schedule()
        buffers: dict = {}
        for st in r.level(parse(text, file), "l2"):
            if r.head(st, machine, machines):
                s.machine = r.machine
                continue
            r.need_machine(st)
            if st.op == "buffer":
                b = self._read_buffer(r, st, s)
                if b.name in buffers or b.name in WORDS:
                    r.fail(st, f"buffer {b.name!r} named twice or a value's name")
                buffers[b.name] = b
            elif st.op == "package":
                (n,) = st.positional() or [None]
                package = r.int(st, n)
                for item in st.body:
                    self._read_item(r, item, s, buffers, package)
            else:
                r.fail(st, f"no L2 statement {st.op!r}")
        return s

    def _read_buffer(self, r, st, s):
        pos, kw = st.positional(), st.kwargs()
        if not pos:
            r.fail(st, "wanted `buffer NAME ...`")
        name, rest = r.name(st, pos[0]), pos[1:]
        layout = None
        if rest and isinstance(rest[0], Call):
            call, rest = rest[0], rest[1:]
            cls = self.layouts.get(call.name)
            if cls is None:
                r.fail(st, f"no layout {call.name!r}")
            if not all(isinstance(a, Kw) for a in call.args):
                r.fail(
                    st, f"a layout's fields are named: {call.name}(field=value, ...)"
                )
            try:
                layout = cls(
                    **{a.name: _read_literal(r, st, a.value) for a in call.args}
                )
            except TypeError as e:
                r.fail(st, str(e))
        base = None
        if rest:
            if len(rest) != 2 or rest[0] != Name("at"):
                r.fail(st, "wanted `at ADDRESS` after the name and layout")
            base = r.int(st, rest[1])
        if set(kw) - {"local", "bytes"}:
            r.fail(
                st,
                f"a buffer has no {', '.join(sorted(set(kw) - {'local', 'bytes'}))}=",
            )
        if "bytes" in kw:
            nbytes = r.int(st, kw["bytes"])
        elif layout is not None and hasattr(layout, "nbytes"):
            nbytes = layout.nbytes
        else:
            r.fail(st, "a buffer without a sized layout wants bytes=")
        space = "mem"
        if "local" in kw:
            space = ("local", r.unit(st, kw["local"]))
            if base is not None:
                r.fail(st, "a unit's local storage has no memory address")
        return s.buffer(name, nbytes, layout, space, base)

    def _read_item(self, r, st, s, buffers, package) -> None:
        pos = st.positional()
        if st.op != "item" or len(pos) != 3 or pos[1] != Name("on"):
            r.fail(st, "wanted `item KIND on UNIT`")
        kind_name, on = r.name(st, pos[0]), r.name(st, pos[2])
        kind = self.kinds.get(kind_name)
        if kind is None:
            r.fail(st, f"no item kind {kind_name!r}")
        if on in r.units:
            at = r.units[on]
            unit = r.types[at]
        else:
            at, unit = None, on
        if unit != kind.unit:
            r.fail(st, f"a {kind_name} runs on a {kind.unit}, not {on}")
        params, reads, writes = {}, [], []
        for line in st.body:
            if line.op == "with":
                if line.positional():
                    r.fail(line, "a parameter is `name=value`")
                params |= {
                    k: _read_value(r, line, v, buffers)
                    for k, v in line.kwargs().items()
                }
            elif line.op in ("read", "write"):
                views = [_read_view(r, line, t, buffers) for t in line.args]
                (reads if line.op == "read" else writes).extend(views)
            else:
                r.fail(line, f"no item line {line.op!r}; with, read or write")
        missing = set(kind.required) - set(params)
        extra = set(params) - set(kind.required) - set(kind.optional)
        if missing or extra:
            r.fail(
                st,
                f"{kind_name}: missing {sorted(missing)}, unknown {sorted(extra)}",
            )
        s.add(kind_name, unit, params, reads, writes, at, package)


def _kw(k: str, v) -> Assign:
    return Assign(Name(k), v)


def _literal(v):
    if isinstance(v, bool):
        return Name(str(v).lower())
    if isinstance(v, int):
        return dec(v)
    if isinstance(v, float):
        return Float(v) if v >= 0 else Neg(Float(-v))
    if isinstance(v, str):
        return Str(v)
    if v is None:
        return Name("none")
    if isinstance(v, tuple):
        return Tuple(tuple(map(_literal, v)))
    raise TypeError(f"no L2 text for the value {v!r}")


def _value(v, addrs):
    if isinstance(v, int) and not isinstance(v, bool):
        named = addrs.name(v)
        if named is not None:
            return named
    if isinstance(v, tuple):
        return Tuple(tuple(_value(x, addrs) for x in v))
    return _literal(v)


def _view(v: View, names) -> object:
    n = names[id(v.buffer)]
    if v.offset == 0 and v.nbytes == v.buffer.nbytes:
        return Name(n)
    return ViewTerm(n, (Slice(num(v.offset), num(v.end)),))


def _read_literal(r, st, t):
    match t:
        case Name(text) if text in WORDS:
            return WORDS[text]
        case Name("inf" | "nan" as text):
            return float(text)
        case Neg(Name("inf")):
            return -math.inf
        case Int() | Neg(Int()):
            return r.int(st, t)
        case Float(v):
            return v
        case Neg(Float(v)):
            return -v
        case Str(text):
            return text
        case Tuple(items):
            return tuple(_read_literal(r, st, x) for x in items)
    r.fail(st, f"wanted a value, got {t!r}")


def _base(r, st, buffers, n: str) -> int:
    b = buffers.get(n)
    if b is None:
        r.fail(st, f"no buffer {n!r}")
    if b.base is None:
        r.fail(st, f"buffer {n!r} has no address")
    return b.base


def _read_value(r, st, t, buffers):
    match t:
        case Name(text) if text not in WORDS:
            return _base(r, st, buffers, text)
        case Offset(Name(text), off):
            return _base(r, st, buffers, text) + r.int(st, off)
        case Tuple(items):
            return tuple(_read_value(r, st, x, buffers) for x in items)
    return _read_literal(r, st, t)


def _read_view(r, st, t, buffers) -> View:
    match t:
        case Name(text) if text in buffers:
            return buffers[text].view()
        case ViewTerm(text, (Slice(lo, hi),)) if text in buffers and lo is not None:
            lo, hi = r.int(st, lo), r.int(st, hi)
            return buffers[text].view(lo, hi - lo)
    r.fail(st, f"wanted a view `buf` or `buf[lo : hi]`, got {t!r}")


__all__ = ["Kind", "L2Text"]
