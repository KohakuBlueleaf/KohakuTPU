"""DSL runners: what a traced kernel records and prints as L2 (DSL.*).

A vector names a kernel by source (exec'd as ``spec_kernel.py`` with ``kt``
and ``np`` in scope) or by example (``vector.softmax:softmax``), the tensor
specs given per port and the constants. The trace machine is read from the
vector's machine file: clusters and vector cores of one die, one node unless
the input says ``nodes``."""

import importlib
import linecache
import re
from contextlib import contextmanager
from dataclasses import replace
from functools import cache

import numpy as np
from harness.registry import mutant, patched, runner

from kohakutpu.application.runtime import device as D
from kohakutpu.language import dsl as kt
from kohakutpu.language import machine as MF
from kohakutpu.language.dsl import expr as E
from kohakutpu.language.dsl import ops as O
from kohakutpu.language.dsl import trace as TR
from kohakutpu.language.dsl import types as TY
from kohakutpu.language.dsl import values as VA
from kohakutpu.stack import kernel as K

#: The kernel module (`dsl.kernel` is the decorator, not the module).
KM = importlib.import_module("kohakutpu.language.dsl.kernel")
#: The file name a vector's kernel source is compiled under (its `file:line`).
FILE = "spec_kernel.py"
EXAMPLES = "kohakutpu.language.examples.dsl"


# ----------------------------------------------------------------- inputs
def trace_machine(inp: dict, ctx: dict) -> kt.Machine:
    """The DSL machine of the vector's machine file."""
    return kt.Machine.of(ctx["machine"], nodes=inp.get("nodes", 1))


@cache
def offline(board: str) -> D.Device:
    """An offline device of `board` (compiles, checks, executes nothing)."""
    return D.Device(kind="offline", board=board)


def namespace(inp: dict, ctx: dict | None = None) -> dict:
    """The source's globals after it ran (its lines in `linecache`, so a
    kernel's `inspect.getsource` reads them)."""
    ns: dict = {
        "kt": kt,
        "np": np,
        "E": E,
        "KM": KM,
        "TY": TY,
        "VA": VA,
        "lines": lines,
    }
    if ctx is not None and ctx.get("machine") is not None:
        ns["MACHINE"] = trace_machine(inp, ctx)
    src = inp["source"]
    linecache.cache[FILE] = (len(src), None, src.splitlines(keepends=True), FILE)
    exec(compile(src, FILE, "exec"), ns)  # noqa: S102  (the vector's kernel)
    return ns


def kernel_of(inp: dict, ctx: dict | None = None) -> kt.Kernel:
    if "example" in inp:
        mod, name = inp["example"].split(":")
        return getattr(importlib.import_module(f"{EXAMPLES}.{mod}"), name)
    return namespace(inp, ctx)[inp.get("kernel", "k")]


def spec(v) -> kt.TensorSpec:
    """A spec from ``[shape, dtype]`` or ``{shape, dtype, layout, space}``."""
    if isinstance(v, list):
        return kt.TensorSpec.make(v[0], v[1] if len(v) > 1 else "f16")
    lay = {
        k: tuple(x) if isinstance(x, list) else x
        for k, x in v.get("layout", {}).items()
    }
    return kt.TensorSpec.make(
        v["shape"], v.get("dtype", "f16"), lay, v.get("space", "dram")
    )


def given(inp: dict) -> dict:
    return {n: spec(v) for n, v in inp.get("given", {}).items()}


def lines(text: str) -> list:
    """The text's lines, an annotation's padding collapsed to one space."""
    return [
        re.sub(r"(?<=\S) {2,}: ", " : ", ln.rstrip())
        for ln in text.rstrip("\n").split("\n")
    ]


def plain(v):
    """`v` as JSON-comparable values."""
    if isinstance(v, dict):
        return {str(k): plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [plain(x) for x in v]
    if isinstance(v, (bool, int, float, str)) or v is None:
        return v
    return repr(v)


def facts(t: kt.Traced, want: list) -> dict:
    got = {
        "text": lambda: t.text,
        "body": lambda: lines(t.text),
        "name": lambda: t.name,
        "params": lambda: [[n, s.text()] for n, s in t.params],
        "roles": lambda: dict(t.roles),
        "consts": lambda: plain(t.consts),
        "machine": lambda: [t.machine.mg, t.machine.vc, t.machine.nodes],
    }
    return {k: got[k]() for k in want}


def stmt_tree(block: list) -> list:
    """A recorded block as plain data: op, args, target, annotation, body,
    and whether it carries its source line."""
    return [
        {
            "op": s.op,
            "args": list(s.args),
            "target": s.target,
            "annot": s.annot,
            "where": s.where or None,
            "body": None if s.body is None else stmt_tree(s.body),
        }
        for s in block
    ]


def walk(block: list) -> list:
    """Every statement of a recorded block, depth first, in program order."""
    out = []
    for s in block:
        out.append(s)
        if s.body is not None:
            out += walk(s.body)
    return out


# ---------------------------------------------------------------- runners
@runner("dsl.trace")
def run_trace(inp: dict, ctx: dict):
    """A kernel traced for the given specs: the facts asked for, or the
    refusal (``{"error", "message"}``)."""
    k = kernel_of(inp, ctx)
    try:
        t = k.trace(
            given(inp), machine=trace_machine(inp, ctx), **inp.get("consts", {})
        )
    except kt.DSLError as e:
        return {"error": type(e).__name__, "message": str(e)}
    return facts(t, inp.get("facts", ["body"]))


@runner("dsl.graph")
def run_graph(inp: dict, ctx: dict):
    """The statement tree and module pieces a trace records (DSL.GRAPH)."""
    seen: list = []

    class Seen(TR.Trace):
        def __init__(self, *a, **kw) -> None:
            super().__init__(*a, **kw)
            seen.append(self)

    k = kernel_of(inp, ctx)
    with patched(KM, "Trace", Seen):
        t = k.trace(
            given(inp), machine=trace_machine(inp, ctx), **inp.get("consts", {})
        )
    tr = seen[-1]
    every = walk(tr.root)
    got = {
        "name": lambda: tr.name,
        "target": lambda: tr.machine.target,
        "ports": lambda: [[n, s.text(), t.roles[n]] for n, s in t.params],
        "scratch": lambda: [[n, s.text()] for n, s in tr.scratch.items()],
        "consts": lambda: plain(t.consts),
        "machine": lambda: [t.machine.mg, t.machine.vc, t.machine.nodes],
        "root": lambda: stmt_tree(tr.root),
        "ops": lambda: [s.op for s in tr.root],
        "allops": lambda: sorted({s.op for s in every}),
        "flat": lambda: [[s.op, list(s.args)] for s in every],
        "targets": lambda: [s.target for s in every if s.target],
        "values": lambda: [[s.target, s.annot] for s in every if s.annot],
    }
    return {k: got[k]() for k in inp.get("keys", ["root"])}


@runner("dsl.spec")
def run_spec(inp: dict, ctx: dict):
    """A buffer spec: its text, bytes, layout and kind, or its refusal."""
    try:
        s = spec(inp)
    except ValueError as e:
        return {"error": str(e)}
    return {
        "text": s.text(),
        "nbytes": s.nbytes(),
        "rowmajor": s.rowmajor,
        "tiled": s.tiled,
        "layout": plain(s.layout),
    }


@runner("dsl.exec")
def run_exec(inp: dict, ctx: dict):
    """The source's ``facts()``: what it observed of the tracer."""
    return plain(namespace(inp, ctx)["facts"]())


@runner("dsl.specialise")
def run_specialise(inp: dict, ctx: dict):
    """`stack.kernel.specialise` on the machine file's board, offline: the
    machine it traced with, the constants it took, the L1 function header,
    and whether a second compile of the same trace is the same object."""
    dev = offline(ctx["vector"]["machine"])
    k = kernel_of(inp, ctx)
    c = K.specialise(k, given(inp), machine=dev.machine, **inp.get("consts", {}))
    again = K.compile_traced(c.traced, dev.machine)
    head = next(ln for ln in c.l1_text.splitlines() if ln.startswith("fn "))
    return {
        "machine": [c.traced.machine.mg, c.traced.machine.vc, c.traced.machine.nodes],
        "consts": plain(c.traced.consts),
        "l1": head.split("(")[0],
        "same": again is c,
        "cached": K.COMPILED_KEPT,
        "store": K.CACHE_DIR.as_posix().split("/")[-2:],
    }


# ---------------------------------------------------------------- mutants
def _wrap(obj, attr: str, make):
    """Patch ``obj.attr`` with ``make(original)``."""
    return patched(obj, attr, make(getattr(obj, attr)))


@contextmanager
def _classmethod(cls, attr: str, fn):
    """Replace classmethod ``cls.attr`` by ``fn(cls, ...)``, restored as it was."""
    keep = cls.__dict__[attr]
    setattr(cls, attr, classmethod(fn))
    try:
        yield
    finally:
        setattr(cls, attr, keep)


@mutant("dsl.trace/target")
def m_target():
    """The trace printed for another target than its machine's."""
    keep = TR.Trace.__init__

    def init(self, name, machine):
        keep(self, name, replace(machine, target="ktpu.v8"))

    with patched(TR.Trace, "__init__", init):
        yield


@mutant("dsl.trace/annot_col")
def m_annot_col():
    with patched(TR, "ANNOT_COL", 40):
        yield


@mutant("dsl.trace/args_comma")
def m_args_comma():
    """Every statement's operands joined by commas."""

    def head(self) -> str:
        lhs = f"{self.target} = " if self.target else ""
        return f"{lhs}{self.op}{' ' + ', '.join(self.args) if self.args else ''}"

    with patched(TR.Stmt, "head", head):
        yield


@mutant("dsl.trace/scratch_last")
def m_scratch_last():
    """Scratch buffers printed after the body."""

    def text(self) -> str:
        params = ", ".join(f"{n}: {s.text()}" for n, s in self.params)
        out = [f"target {self.machine.target}", "", f"fn {self.name}.l2({params})"]
        body = self.root + [
            TR.Stmt("buffer", [], n, s.text()) for n, s in self.scratch.items()
        ]
        return "\n".join(out + TR._block(body, 1)) + "\n"

    with patched(TR.Trace, "text", text):
        yield


@mutant("dsl.trace/indent")
def m_indent():
    with _wrap(
        TR, "_block", lambda f: lambda s, d: [x[2:] if d > 1 else x for x in f(s, d)]
    ):
        yield


@mutant("dsl.trace/no_where")
def m_no_where():
    with patched(TR, "at_caller", lambda: ""):
        yield


@mutant("dsl.kernel/name")
def m_name():
    def init(f):
        def go(self, fn, name=None):
            f(self, fn, name)
            self.name += "_k"

        return go

    build = KM.Kernel.__dict__["build"].__func__
    with (
        _wrap(KM.Kernel, "__init__", init),
        _classmethod(
            KM.Kernel, "build", lambda c, n, *a, **kw: build(c, n + "_k", *a, **kw)
        ),
    ):
        yield


@mutant("dsl.kernel/in_is_out")
def m_in_is_out():
    with patched(kt, "In", kt.Out), patched(KM, "In", kt.Out):
        yield


@mutant("dsl.kernel/given_shape")
def m_given_shape():
    """A given tensor's shape read reversed."""
    keep = KM._spec_of

    def spec_of(x):
        s = keep(x)
        return s.with_(shape=s.shape[::-1])

    with patched(KM, "_spec_of", spec_of):
        yield


@mutant("dsl.kernel/port_dtype")
def m_port_dtype():
    """A port's spec takes the given tensor's dtype, not the port's."""

    def specs(f):
        def go(self, g, consts):
            ports = {n: p for n, p in self.ports.items()}
            keep = {n: p.dtype for n, p in ports.items()}
            try:
                for p in ports.values():
                    p.dtype = None
                return f(self, g, consts)
            finally:
                for n, p in ports.items():
                    p.dtype = keep[n]

        return go

    with _wrap(KM.Kernel, "specs", specs):
        yield


@mutant("dsl.kernel/no_cache")
def m_no_cache():
    """Every trace runs the body again."""

    def tr(f):
        def go(self, g, consts, machine):
            self._cache.clear()
            KM._TRACES.clear()
            return f(self, g, consts, machine)

        return go

    with _wrap(KM.Kernel, "_trace", tr):
        yield


@mutant("dsl.kernel/plain_none")
def m_plain_none():
    with patched(KM, "_plain", lambda v: False):
        yield


@mutant("dsl.kernel/plain_all")
def m_plain_all():
    with patched(KM, "_plain", lambda v: True):
        yield


@mutant("dsl.kernel/auto_reversed")
def m_auto_reversed():
    class Rev(KM.Auto):
        def __init__(self, *c) -> None:
            super().__init__(*c[::-1])

    with patched(KM, "Auto", Rev):
        yield


@mutant("dsl.kernel/auto_causes_dropped")
def m_auto_causes_dropped():
    """A failed `kt.auto` names no cause, only that no candidate passed."""
    with patched(KM, "causes", lambda errors: f"{len(errors)} candidates"):
        yield


@mutant("dsl.kernel/machine_default")
def m_machine_default():
    """`kt.machine()` answers another machine, not the traced one."""
    other = kt.Machine.of(MF.load("multimesh_v9t8"))
    with patched(kt, "machine", lambda: other):
        yield


@mutant("dsl.kernel/machine_optional")
def m_machine_optional():
    """A trace without a machine traces for v9t6's."""
    keep = KM.Kernel.trace

    def trace(self, given=None, *, machine=None, check=None, **consts):
        machine = machine or kt.Machine.of(MF.load("multimesh_v9t6"))
        return keep(self, given, machine=machine, check=check, **consts)

    with patched(KM.Kernel, "trace", trace):
        yield


@mutant("dsl.trace/lanes_16")
def m_lanes_16():
    """The tracer bounds lanes by 16, not the machine's `lanes`."""
    keep = TR.Trace.__init__

    def init(self, name, machine):
        keep(self, name, replace(machine, lanes=16))

    with patched(TR.Trace, "__init__", init):
        yield


@mutant("dsl.kernel/require_off")
def m_require_off():
    with (
        patched(O, "require", lambda cond, why: None),
        patched(kt, "require", lambda cond, why: None),
    ):
        yield


@mutant("dsl.compile/dsl_machine")
def m_dsl_machine():
    with _wrap(
        K,
        "dsl_machine",
        lambda f: lambda m: replace(f(m), mg=f(m).vc, vc=f(m).mg + 1, nodes=1),
    ):
        yield


@mutant("dsl.compile/no_memo")
def m_no_memo():
    with patched(K, "COMPILED_KEPT", 0):
        yield


@mutant("dsl.types/text")
def m_types_text():
    def text(self) -> str:
        dims = "x".join(str(d) for d in self.shape)
        return f"{self.dtype}[{dims}] @{self.space}"

    with patched(TY.TensorSpec, "text", text):
        yield


@mutant("dsl.types/item")
def m_item():
    with patched(TY, "ITEM", {**TY.ITEM, "f16": 1, "f32": 2}):
        yield


@mutant("dsl.types/dtypes")
def m_dtypes():
    with patched(TY, "DTYPES", ("f16", "mx7a")):
        yield


@mutant("dsl.types/spaces")
def m_spaces():
    with patched(TY, "SPACES", ("stage", "mem")):
        yield


@mutant("dsl.types/layout_keys")
def m_layout_keys():
    with patched(TY, "LAYOUT_KEYS", {"mx7a": {"gn", "nk"}, "mx7b": {"gm", "nk"}}):
        yield


@mutant("dsl.types/rowmajor")
def m_rowmajor():
    with patched(TY.TensorSpec, "rowmajor", property(lambda s: not s.layout)):
        yield


@mutant("dsl.types/unsorted")
def m_unsorted():
    def make(cls, shape, dtype="f16", layout=None, space="dram"):
        lay = tuple(sorted((layout or {}).items(), reverse=True))
        s = cls(tuple(int(d) for d in shape), dtype, lay, space)
        s.check()
        return s

    with _classmethod(TY.TensorSpec, "make", make):
        yield


@mutant("dsl.values/view_text")
def m_view_text():
    def text(self) -> str:
        parts = [
            ":" if a[0] == "all" else f"{E.text(a[1])}:{a[2]}"
            for a in self.axes
            if a[0] != "new"
        ]
        return f"{self.param.name}[{', '.join(parts)}]"

    with patched(VA.View, "text", text):
        yield


@mutant("dsl.values/view_shape")
def m_view_shape():
    with patched(VA.View, "shape", property(lambda s: tuple(s.param.shape))):
        yield


@mutant("dsl.values/value_text")
def m_value_text():
    with patched(VA.ValueView, "_cols", lambda self, a: False):
        yield


@mutant("dsl.values/stat")
def m_stat():
    with patched(VA.Value, "stat", property(lambda s: False)):
        yield


@mutant("dsl.expr/text_order")
def m_text_order():
    def text(self) -> str:
        parts = [
            f"{a.text()}*{c}" if c != 1 else a.text() for a, c in self.terms.items()
        ]
        if self.const:
            parts.append(str(self.const))
        return " + ".join(parts[::-1])

    with patched(E.Sym, "text", text):
        yield


@mutant("dsl.expr/floordiv")
def m_floordiv():
    with patched(E.Sym, "__floordiv__", lambda s, o: E.Sym({E.Atom("/", s, o): 1})):
        yield


@mutant("dsl.expr/scale")
def m_scale():
    with patched(E.Sym, "__mul__", lambda s, o: E.Sym({E.Atom("*", s, o): 1})):
        yield


@mutant("dsl.expr/cond")
def m_cond():
    with patched(E.Sym, "__eq__", lambda s, o: E.Cond("!=", s, o)):
        yield


@mutant("dsl.ops/var_names")
def m_var_names():
    with patched(O, "_var_name", lambda t, kind: t.fresh("p")):
        yield


@mutant("dsl.ops/loop_prefix")
def m_loop_prefix():
    with _wrap(
        TR.Trace,
        "fresh",
        lambda f: lambda self, p: f(self, {"t": "k", "k": "t"}.get(p, p)),
    ):
        yield


@mutant("dsl.ops/value_prefix")
def m_value_prefix():
    def value(f):
        def go(*a, prefix="v", **kw):
            return f(*a, prefix=prefix + "z", **kw)

        return go

    with _wrap(O, "_value", value):
        yield


@mutant("dsl.ops/compare_prefix")
def m_compare_prefix():
    with patched(O, "_PREFIX", {}):
        yield


@mutant("dsl.ops/scope_swap")
def m_scope_swap():
    swap = {"mg": "vc", "vc": "mg"}
    with _wrap(O, "_need", lambda f: lambda kind, what: f(swap.get(kind, kind), what)):
        yield


@mutant("dsl.ops/unit_kind")
def m_unit_kind():
    """A par's unit printed as the other kind."""
    swap = {"mg": "vc", "vc": "mg"}

    def swapped(a) -> str:
        kind, sep, rest = str(a).partition("[")
        return f"{swap.get(kind, kind)}{sep}{rest}"

    def emit(f):
        def go(self, op, *args, **kw):
            if op == "par":
                args = tuple(swapped(a) for a in args)
            return f(self, op, *args, **kw)

        return go

    with _wrap(TR.Trace, "emit", emit):
        yield


@mutant("dsl.ops/unpack_again")
def m_unpack_again():
    def unpack(f):
        def go(x, *, lane=None):
            if isinstance(x, VA.Value):
                x.unpacked = None
            return f(x, lane=lane)

        return go

    with _wrap(O, "unpack", unpack):
        yield


@mutant("dsl.ops/no_broadcast")
def m_no_broadcast():
    with _wrap(O, "_operand", lambda f: lambda r, broadcast: f(r, False)):
        yield


@mutant("dsl.ops/number_text")
def m_number_text():
    with _wrap(
        O,
        "_operand",
        lambda f: lambda r, broadcast: (
            f"{r:g}" if isinstance(r, float) else f(r, broadcast)
        ),
    ):
        yield


@mutant("dsl.ops/math_shape")
def m_math_shape():
    """Math takes its first operand's shape."""

    def value(f):
        def go(op, args, dtype, shape, space, *a, **kw):
            if op in O.UNARY + O.BINARY + O.TERNARY + O.COMPARE:
                shape = (
                    tuple(shape)[:1] + (min(tuple(shape)[-1], 32),)
                    if len(shape) == 2
                    else shape
                )
            return f(op, args, dtype, shape, space, *a, **kw)

        return go

    with _wrap(O, "_value", value):
        yield


@mutant("dsl.ops/op_names")
def m_op_names():
    """`recip` records `rsqrt` (one named op mapped wrong)."""
    with patched(O, "recip", O.rsqrt), patched(kt, "recip", O.rsqrt):
        yield


@mutant("dsl.ops/xlane_rot")
def m_xlane_rot():
    with patched(kt, "xlane", O.xrot), patched(kt, "xrot", O.xlane):
        yield


@mutant("dsl.ops/gt4_off")
def m_gt4_off():
    """A load is never GT4."""

    def load(f):
        def go(view, *, gt4=None):
            v = f(view, gt4=gt4)
            if v.mode == "gt4":
                v.mode = None
                v.stmt.annot = v.type_text()
            return v

        return go

    with _wrap(O, "load", load), _wrap(kt, "load", load):
        yield


@mutant("dsl.ops/pack_gt4_ignored")
def m_pack_gt4_ignored():
    """A pack's `gt4` argument ignored."""

    def pack(f):
        def go(x, dtype="f16", *, gt4=None):
            return f(x, dtype)

        return go

    with _wrap(O, "pack", pack), _wrap(kt, "pack", pack):
        yield


@mutant("dsl.ops/for_scope_node")
def m_for_scope_node():
    """A `for` opens node scope."""

    def scope(kind=None, loop=False, sticky=False, node=False):
        return TR.Scope(None if loop else kind, loop, sticky, node)

    with patched(O, "Scope", scope):
        yield


@mutant("dsl.ops/node_scope_unit")
def m_node_scope_unit():
    """A par over nodes opens a vector-core instance."""

    def scope(kind=None, loop=False, sticky=False, node=False):
        return TR.Scope("vc" if node and kind is None else kind, loop, sticky, node)

    with patched(O, "Scope", scope):
        yield


@mutant("dsl.ops/node_scope_vc")
def m_node_scope_vc():
    """Node-scope statements asked of a vector core."""
    with _wrap(O, "_need", lambda f: lambda kind, what: f(kind or "vc", what)):
        yield


@mutant("dsl.ops/no_compare")
def m_no_compare():
    with patched(O, "COMPARE", ()):
        yield


@mutant("dsl.values/sub_add")
def m_sub_add():
    with patched(VA.Value, "__sub__", VA.Value.__add__):
        yield


@mutant("dsl.values/unit_text")
def m_unit_text():
    with patched(VA.Unit, "text", lambda self: f"{self.kind}:{self.index}"):
        yield


@mutant("dsl.kernel/body_twice")
def m_body_twice():
    """A trace runs the body twice."""

    def tr(f):
        def go(self, g, consts, machine):
            fn = self.fn

            def twice(**kw):
                fn(**kw)
                fn(**kw)

            self.fn = twice
            try:
                return f(self, g, consts, machine)
            finally:
                self.fn = fn

        return go

    with _wrap(KM.Kernel, "_trace", tr):
        yield


@mutant("dsl.ops/gemm_fresh")
def m_gemm_fresh():
    """A gemm into `acc` takes a fresh name."""
    with (
        _wrap(O, "gemm", lambda f: lambda a, b, acc=None: f(a, b)),
        _wrap(kt, "gemm", lambda f: lambda a, b, acc=None: f(a, b)),
    ):
        yield


@mutant("dsl.ops/mover_flags")
def m_mover_flags():
    """Every mover keyword printed, None and True as values."""

    def mover(op, dst, src=None, **kw):
        args = [VA.as_view(dst).text()]
        if src is not None:
            args += ["<-", VA.as_view(src).text()]
        TR.active().emit(op, *args, *[f"{k}={v}" for k, v in kw.items()])

    with patched(O, "_mover", mover):
        yield


@mutant("dsl.ops/walk_text")
def m_walk_text():
    with patched(
        O, "_walk_text", lambda w: None if w is None else str(list(map(list, w)))
    ):
        yield


@mutant("dsl.ops/store_order")
def m_store_order():
    """A store printed as `store P -> VIEW`."""

    def emit(f):
        def go(self, op, *args, **kw):
            if op == "store":
                args = (args[2], "->", args[0])
            return f(self, op, *args, **kw)

        return go

    with _wrap(TR.Trace, "emit", emit):
        yield


@mutant("dsl.ops/drain_arrow")
def m_drain_arrow():
    def emit(f):
        def go(self, op, *args, **kw):
            if op in ("drain", "copy", "quant", "gt4", "gather", "scatter"):
                args = tuple("=>" if a in ("->", "<-") else a for a in args)
            return f(self, op, *args, **kw)

        return go

    with _wrap(TR.Trace, "emit", emit):
        yield


@mutant("dsl.ops/statistic-band-pack")
def m_statistic_band_pack():
    """Re-admit the 32-column statistic pack."""
    original = O.pack

    def pack(x, dtype="f16", *, gt4=None):
        if x.stat and gt4:
            return O._value(
                "pack",
                [x.name],
                dtype,
                (x.shape[0], 32),
                "l1",
                "gt4",
                prefix="p",
                nodes=[O.G.value(x.name)],
            )
        return original(x, dtype, gt4=gt4)

    with patched(O, "pack", pack), patched(kt, "pack", pack):
        yield


__all__ = ["FILE", "given", "kernel_of", "lines", "offline", "spec", "trace_machine"]
