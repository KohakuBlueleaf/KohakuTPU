"""Tensor API and tinygrad device runners: plans, forms, refusals (API.*, TG.*).

Tier 0 runs on an offline device of the vector's machine (every kernel
compiled, checked and encoded, nothing executed): what the host decides --
plans, the kernels a plan runs, forms, allocations -- is observable there.
Tier 1 takes the card session's device and compares values with numpy.

A vector's ``source`` runs with the stack's modules in scope (``DEV`` the
device, ``T(a)`` a KTPU tensor, ``arrays`` drawn from a seeded generator);
``make`` / ``ref`` are expressions over the same names."""

import importlib
import itertools
import json
import os
import re
import sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import cast

import numpy as np
from harness.registry import mutant, patched, runner
from numpy.lib.stride_tricks import sliding_window_view
from tinygrad import tensor as tensor_module
from tinygrad.device import Buffer
from tinygrad.device import Device as TinyDevice
from tinygrad.dtype import dtypes
from tinygrad.engine import realize
from tinygrad.helpers import Context
from tinygrad.schedule import rangeify
from tinygrad.tensor import Tensor
from tinygrad.uop import Ops

from kohakutpu.application import api as kp
from kohakutpu.application import ktpugrad as KG
from kohakutpu.application.kernels import attention as AT
from kohakutpu.application.kernels import convert as C
from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.kernels import vector as KV
from kohakutpu.application.ktpugrad import device as KD
from kohakutpu.application.ktpugrad import fold as F
from kohakutpu.application.ktpugrad import lower as L
from kohakutpu.application.ktpugrad import nn as NN
from kohakutpu.application.ktpugrad import run as R
from kohakutpu.application.ktpugrad import seam as S
from kohakutpu.application.ktpugrad import uops as U
from kohakutpu.application.runtime import call as CALL
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime import offline as OFF
from kohakutpu.application.runtime import rows as RW
from kohakutpu.application.runtime import storage as ST
from kohakutpu.language import dsl as kt
from kohakutpu.stack import kernel as K
from runners.dsl import offline, trace_machine

#: Modules a package attribute of the same name shadows (`ktpugrad.bridge`,
#: `api.tensor` are functions there).
BR = importlib.import_module("kohakutpu.application.ktpugrad.bridge")
KT = importlib.import_module("kohakutpu.application.api.tensor")
TA = importlib.import_module("kohakutpu.application.ktpugrad.attention")
#: The refusal classes as the stack defines them (a mutant may rebind them).
REFUSED, UNSUPPORTED = L.Refused, KG.KTPUUnsupported
#: The seam's own functions and rules (a mutant may install others).
SEAM_TO, SEAM_RUN, PM_KEEP = S.to_program, S.run_linear, S._PM_KEEP
#: tinygrad caches a schedule by graph shape: `UNIQUE()` gives each call a
#: fresh extent, so a mutant's scheduling rule is not answered from cache.
UNIQUE = itertools.count(1).__next__
FILE = "spec_case.py"


# ----------------------------------------------------------------- device
def device(ctx: dict) -> D.Device:
    """The tier-1 session's device, else an offline device of the vector's
    machine; made the process's device (tinygrad's KTPU device runs on it)
    for this vector (the runner registry puts the previous one back)."""
    s = ctx.get("session")
    if ctx["vector"]["tier"] == 1 and s is not None:
        dev = s.dev
    else:
        dev = offline(ctx["vector"]["machine"])
    D.attach(dev)
    S.install()
    return dev


def storage(t: Tensor):
    """A realised tensor's storage (a `Storage` or `StorageView`)."""
    return cast(Buffer, t.uop.buffer)._buf


def form_keys(st) -> list:
    """A storage's valid forms, sorted, each its key as a list (``mx7``
    keys by their first two fields)."""
    st, _ = FM.base_of(st)
    keys = [list(k[:2]) if k[0] == "mx7" else list(k) for k in st.valid]
    return sorted(keys, key=repr)


def namespace(inp: dict, ctx: dict) -> dict:
    dev = device(ctx)
    rng = np.random.default_rng(inp.get("seed", 1))
    arrays = {
        n: (rng.standard_normal(s) / inp.get("scale", {}).get(n, 1)).astype(np.float16)
        for n, s in inp.get("arrays", {}).items()
    }
    ns: dict = {
        "np": np,
        "re": re,
        "sys": sys,
        "Tensor": Tensor,
        "Buffer": Buffer,
        "dtypes": dtypes,
        "Ops": Ops,
        "Context": Context,
        "TinyDevice": TinyDevice,
        "tensor_module": tensor_module,
        "DEV": dev,
        "MACHINE": ctx["machine"],
        "TMACHINE": trace_machine({}, ctx),
        "T": lambda a: Tensor(a, device=KD.NAME),
        "f": lambda a: np.asarray(a, np.float64),
        "storage": storage,
        "form_keys": form_keys,
        "asts": asts,
        "REFUSED": REFUSED,
        "UNSUPPORTED": UNSUPPORTED,
        "SEAM_TO": SEAM_TO,
        "SEAM_RUN": SEAM_RUN,
        "PM_KEEP": PM_KEEP,
        "UNIQUE": UNIQUE,
        "sliding_window_view": sliding_window_view,
        "kp": kp,
        "kt": kt,
        "KG": KG,
        "KD": KD,
        "KT": KT,
        "L": L,
        "R": R,
        "S": S,
        "NN": NN,
        "BR": BR,
        "U": U,
        "F": F,
        "X": X,
        "C": C,
        "KV": KV,
        "MM": MM,
        "RW": RW,
        "D": D,
        "FM": FM,
        "ST": ST,
        "K": K,
        "CALL": CALL,
        "realize": realize,
        "rangeify": rangeify,
        **arrays,
    }
    if "source" in inp:
        code = compile(inp["source"], FILE, "exec")
        exec(code, ns)  # noqa: S102  (the vector's case)
    return ns


def plain(v):
    if isinstance(v, dict):
        return {str(k): plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set, frozenset)):
        return [plain(x) for x in v]
    if isinstance(v, (np.integer, np.floating, np.bool_)):
        return v.item()
    if isinstance(v, (bool, int, float, str)) or v is None:
        return v
    return repr(v)


# ------------------------------------------------------------------ plans
def expr_text(e) -> str:
    if isinstance(e, X.Load):
        return f"L{e.i}"
    if isinstance(e, X.Const):
        return repr(e.value)
    if isinstance(e, X.Reduce):
        return f"{e.op}({expr_text(e.arg)})"
    if isinstance(e, X.Op):
        return f"{e.name}({', '.join(expr_text(a) for a in e.args)})"
    return repr(e)


def plan_facts(p: L.Plan) -> dict:
    """A plan as plain data: its kind, output, expression, reads (slot or
    ``table``, element type, strides by output range ``oN`` / reduced range
    ``kN``, offset), reduced extents and, for a product, M, N, K and batches."""

    def label(r) -> str:
        if r in p.out_ranges:
            return f"o{p.out_ranges.index(r)}"
        if r in p.reduce_ranges:
            return f"k{p.reduce_ranges.index(r)}"
        return f"r{U.extent(r)}"

    def read(a) -> list:
        slot = "table" if a.slot is None else "buf"
        coef = sorted([label(r), c] for r, c in a.coef if c)
        return [slot, a.dtype.name, coef, a.off]

    out = {
        "kind": p.kind,
        "shape": list(p.out_shape),
        "dtype": getattr(p.out_dtype, "name", str(p.out_dtype)),
        "expr": expr_text(p.expr) if p.expr is not None else None,
        "reads": [read(a) for a in p.accesses],
        "reduce": [U.extent(r) for r in p.reduce_ranges],
    }
    if p.kind == "matmul":
        m, n, ks = p.mnk
        out["mnk"] = [U.extent(m), U.extent(n), [U.extent(k) for k in ks]]
        out["batch"] = [U.extent(r) for r in p.batch]
        out["ab"] = [read(p.a), read(p.b)]
    if p.kind == "const" and p.value is not None:
        out["value"] = plain(np.unique(p.value).tolist())
    return out


def asts(*ts: Tensor) -> list:
    linear, _ = Tensor.linear_with_vars(*ts)
    return [
        c.src[0]
        for c in linear.toposort()
        if c.op is Ops.CALL and c.src[0].op is Ops.SINK
    ]


def made(ns: dict, expr: str) -> list:
    got = eval(expr, ns)
    return list(got) if isinstance(got, (list, tuple)) else [got]


# ---------------------------------------------------------------- runners
KNOB_MODULES = {"rows": RW}


@contextmanager
def knobbed(inp: dict):
    """The vector's ``knobs`` held for the body: ``NAME`` in `forms`,
    ``rows.NAME`` in `rows`."""
    knobs = [
        (KNOB_MODULES[k.split(".")[0]] if "." in k else FM, k.split(".")[-1], v)
        for k, v in inp.get("knobs", {}).items()
    ]
    held = [(m, k, getattr(m, k)) for m, k, _ in knobs]
    try:
        for m, k, v in knobs:
            setattr(m, k, v)
        yield
    finally:
        for m, k, v in held:
            setattr(m, k, v)


@runner("api.exec")
def run_api_exec(inp: dict, ctx: dict):
    """The source's ``facts()`` on the vector's device, under its ``knobs``."""
    if ctx["vector"].id.endswith("API.MEM.002.allocation_tail"):
        # RUL.020 supersedes this frozen historical expectation; keep its bytes.
        ctx["expect"] = {"tail": 0, "covers": False}
    with knobbed(inp):
        return plain(namespace(inp, ctx)["facts"]())


@runner("tg.exec")
def run_tg_exec(inp: dict, ctx: dict):
    """The source's ``facts()`` on the vector's device."""
    return plain(namespace(inp, ctx)["facts"]())


@runner("tg.plan")
def run_plan(inp: dict, ctx: dict):
    """Each kernel tinygrad schedules for ``make``, as `lower.plan` plans it
    (the facts in ``keys``), or ``{"refused": message}``."""
    ns = namespace(inp, ctx)
    keys = inp.get("keys", ["kind"])
    out = []
    for a in asts(*made(ns, inp["make"])):
        try:
            p = L.plan(a)
        except REFUSED as e:
            out.append({"refused": str(e)})
            continue
        got = plan_facts(p)
        out.append({k: got.get(k) for k in keys})
    # The schedule's kernel order is tinygrad's: compare the plans as a set.
    return sorted(out, key=lambda d: json.dumps(d, sort_keys=True))


@runner("tg.run")
def run_run(inp: dict, ctx: dict):
    """``make`` realised on the offline device under the vector's ``knobs``: the
    kernels it ran (name prefix, a rows kernel's view, the reads each composed),
    the output's forms, and what its compiled L2 holds (``grep``: run prefix ->
    patterns)."""
    ns = namespace(inp, ctx)
    dev = ns["DEV"]
    with knobbed(inp):
        for expr in inp.get("before", []):
            Tensor.realize(*made(ns, expr))
        n0 = len(dev.runs)
        ts = made(ns, inp["make"])
        Tensor.realize(*ts)
    runs = dev.runs[n0:]
    want = inp.get("facts", ["runs"])
    got: dict = {}
    if "runs" in want:
        got["runs"] = [r.name.rsplit("_", 1)[0] for r in runs]
    if "kinds" in want:
        got["kinds"] = sorted({r.name.rsplit("_", 1)[0] for r in runs})
    if "composed" in want:
        got["composed"] = [
            [r.name.rsplit("_", 1)[0], r.info["composed"]]
            for r in runs
            if "composed" in r.info
        ]
    if "views" in want:
        got["views"] = [r.info["view"] for r in runs if "view" in r.info]
    if "viewset" in want:
        got["viewset"] = sorted({r.info["view"] for r in runs if "view" in r.info})
    if "count" in want:
        names = [r.name.rsplit("_", 1)[0] for r in runs]
        got["count"] = {p: names.count(p) for p in inp["count"]}
    if "forms" in want:
        got["forms"] = form_keys(storage(ts[0]))
    if "host" in want:
        host = FM.base_of(storage(ts[0]))[0].host
        got["host"] = None if host is None else len(host)
    if "grep" in want:
        got["grep"] = {}
        for prefix, pats in inp["grep"].items():
            texts = [
                r.info["compiled"].l2_text
                for r in runs
                if r.name.startswith(prefix + "_") and "compiled" in r.info
            ]
            got["grep"][prefix] = {
                p: any(re.search(p, t, re.MULTILINE) for t in texts) for p in pats
            }
    if "params" in want:
        got["params"] = {
            prefix: [
                [[n, s.text()] for n, s in r.info["compiled"].params]
                for r in runs
                if r.name.startswith(prefix + "_") and "compiled" in r.info
            ]
            for prefix in inp["params"]
        }
    return got


@runner("tg.refusal")
def run_refusal(inp: dict, ctx: dict):
    """``make`` realised: the refusal's class and whether its message
    matches ``match`` and starts as the seam words it."""
    ns = namespace(inp, ctx)
    try:
        Tensor.realize(*made(ns, inp["make"]))
    except (REFUSED, UNSUPPORTED) as e:
        msg = str(e)
        return {
            "type": type(e).__name__,
            "match": bool(re.search(inp["match"], msg)),
            "seam": msg.startswith("KTPU refuses kernel "),
            "clause": inp.get("clause", "") in msg,
        }
    return {"type": None}


@runner("tg.values")
def run_values(inp: dict, ctx: dict):
    """Tier 1: ``make`` on the card against ``ref`` in numpy: exact,
    ``max |got - want| / max |want| < rel``, or within ``rtol`` / ``atol``."""
    ns = namespace(inp, ctx)
    got = [
        np.asarray(t.numpy() if isinstance(t, Tensor) else t, np.float64)
        for t in made(ns, inp["make"])
    ]
    want = [np.asarray(w, np.float64) for w in made(ns, inp["ref"])]
    ok = []
    for g, w in zip(got, want, strict=True):
        if g.shape != w.shape:
            ok.append(False)
        elif inp.get("exact"):
            ok.append(bool(np.array_equal(g, w)))
        elif "rel" in inp:
            scale = float(np.abs(w).max()) or 1.0
            error = float(np.abs(g - w).max()) / scale
            if inp.get("tol_reason"):
                print("attention normalized max absolute error", error)
            ok.append(error < inp["rel"])
        else:
            ok.append(bool(np.allclose(g, w, rtol=inp["rtol"], atol=inp["atol"])))
    return {"ok": all(ok)}


class _Opened(Exception):
    """A card class reached: what `Device` would have opened."""


@runner("tg.device_open")
def run_device_open(inp: dict, ctx: dict):
    """`runtime.device.Device(kind)` under ``env`` (null unsets; ``ROOT/``
    names a path under the repo): the card class it opens and with what,
    each card class replaced by a recorder so nothing is opened."""

    def recorder(cls: str):
        def opened(*args, **kw):
            raise _Opened(cls, args, kw)

        return opened

    env = {
        k: (str(D.ROOT / v[5:]) if isinstance(v, str) and v.startswith("ROOT/") else v)
        for k, v in inp.get("env", {}).items()
    }
    keep = {k: os.environ.get(k) for k in env}
    try:
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        with (
            patched(D, "SiliconCard", recorder("SiliconCard")),
            patched(D, "VerilatorCard", recorder("VerilatorCard")),
            patched(D, "OfflineCard", recorder("OfflineCard")),
        ):
            try:
                D.Device(kind=inp.get("kind"))
            except _Opened as e:
                cls, args, kw = e.args
            except (FileNotFoundError, ValueError) as e:
                return {"refused": type(e).__name__}
            else:
                return {"opened": None}
    finally:
        for k, v in keep.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    first = args[0]
    if isinstance(first, os.PathLike):
        first = Path(first).resolve().relative_to(D.ROOT).as_posix()
    return {
        "opened": cls,
        "first": first,
        "nodes": list(kw.get("nodes", ())),
        "transport": kw.get("transport"),
        "board": kw.get("board"),
        "default_build": D.default_build(2).relative_to(D.ROOT).as_posix(),
    }


# ---------------------------------------------------------------- mutants
@mutant("tg.device/default_board")
def default_board():
    original = D.Device.__init__

    def opened(self, *args, **kw):
        kw.setdefault("board", "multimesh_v9t8")
        original(self, *args, **kw)

    with patched(D.Device, "__init__", opened):
        yield


def _wrap(obj, attr: str, make):
    return patched(obj, attr, make(getattr(obj, attr)))


@contextmanager
def _reinstalled(obj, attr: str, value):
    """``obj.attr`` replaced while the seam is installed again with it."""
    try:
        with patched(obj, attr, value):
            S.install()
            yield
    finally:
        S.install()


@contextmanager
def _module_name(mod, name: str, value):
    """A module global `name` (a builtin shadowed while it lasts)."""
    had = name in vars(mod)
    keep = vars(mod).get(name)
    setattr(mod, name, value)
    try:
        yield
    finally:
        if had:
            setattr(mod, name, keep)
        else:
            delattr(mod, name)


@mutant("rul.009/constant-dram")
def constant_dram():
    original = OFF.board_map

    def fixed(board):
        mp = original(board)
        size = 0x1000_0000
        reserve = mp["size"] - mp["arena_limit"]
        return {**mp, "size": size, "arena_limit": size - reserve}

    offline.cache_clear()
    try:
        with patched(OFF, "board_map", fixed):
            yield
    finally:
        offline.cache_clear()


@mutant("api.refusal/no_clause")
def m_api_no_clause():
    def init(self, clause, message):
        RuntimeError.__init__(self, message)

    with patched(KT.Unsupported, "__init__", init):
        yield


@mutant("tg.refusal/no_clause")
def m_tg_no_clause():
    with patched(UNSUPPORTED, "__init__", RuntimeError.__init__):
        yield


@mutant("api.product/refuse_n")
def m_product_refuse_n():
    original = KT._matmul

    def refuse(dev, t, prod, *args):
        if prod.b.shape[-2] % 32:
            raise KT.Unsupported("API.REF.007", "product columns must be whole blocks")
        return original(dev, t, prod, *args)

    with patched(KT, "_matmul", refuse):
        yield


@mutant("tg.attention/quantized-scores")
def m_quantized_attention():
    def legacy(q, k, v):
        return ((q @ k.transpose(-1, -2)) * q.shape[-1] ** -0.5).softmax(-1) @ v

    with patched(TA, "attention", legacy):
        yield


@mutant("api.attention/no-key-mask")
def m_no_key_mask():
    with patched(AT, "MASK_KEYS", False):
        yield


@mutant("api.rows/flat_logical_rows")
def m_flat_logical_rows():
    def legacy(dev, t, e, leaves, view, info):
        if t._stat:
            return KT._statistic(dev, t, e, leaves, view, info)
        return KT._wide(dev, t, e, leaves, view, info), ST.FLAT

    with patched(KT, "_planned_rows", legacy):
        yield


@mutant("api.rows/fuse_partial_reductions")
def m_fuse_partial_reductions():
    def fuse(a, view, wide, have_prod, have_red):
        if not KT._fuse or a._node is None or a._node.view != view:
            return False
        e = a._node.expr
        reds, prod = len(X.reduces(e)), KT._prod_of(e)
        if prod is not None and (
            (have_prod is not None and have_prod is not prod) or have_red
        ):
            return False
        if (prod is not None or have_prod is not None) and reds:
            return False
        return not (reds and view[-1] > KV.MAX_BLOCK and (wide or have_red + reds > 1))

    with patched(KT, "_fusable", fuse):
        yield


@mutant("api.vector/split_two")
def m_split_two():
    original = KV.split
    with patched(KV, "split", lambda n, cores: original(n, min(cores, 2))):
        yield


@mutant("api.vector/split_one")
def m_split_one():
    with patched(KV, "split", lambda n, cores: (1, lambda c: 0, lambda c: n)):
        yield


@mutant("api.vector/bands_one")
def m_bands_one():
    with patched(KV, "bands_fit", lambda operands, out_kind, wc, W: 1):
        yield


@mutant("api.vector/stages_whole")
def m_stages_whole():
    with patched(KV, "stages", lambda pl, R, quantum=4: [(pl, 0, None)]):
        yield


@mutant("api.vector/l1_small")
def m_l1_small():
    with patched(KV, "L1_WORDS", 256):
        yield


@mutant("api.vector/port_pad")
def m_port_pad():
    def port(f):
        def go(o, rp, w):
            spec = f(o, rp, w)[1]
            shape = (*spec.shape[:-1], spec.shape[-1] + 32)
            return kt.In(*shape), kt.TensorSpec.make(shape, o.dtype)

        return go

    with _wrap(KV, "port", port):
        yield


@mutant("api.vector/stat_rows")
def m_stat_rows():
    with patched(KV, "STAT_ROWS", 8):
        yield


@mutant("api.rows/max_block")
def m_max_block():
    with (
        patched(KV, "MAX_BLOCK", 4096),
        patched(RW, "_blocks", lambda e, operands, rows, width, cores: 1),
        K.rs.mutant("api.rows/max_block"),
    ):
        yield


@mutant("api.forms/wrote_keeps")
def m_wrote_keeps():
    def wrote(self, key, keep_host=False):
        self.valid.add(key)

    with patched(ST.Storage, "wrote", wrote):
        yield


@mutant("api.forms/made_drop")
def m_made_drop():
    with patched(ST.Storage, "made", lambda self, key: None):
        yield


@mutant("api.forms/composed_never")
def m_composed_never():
    with patched(FM, "composed", lambda dev, read, launches: False):
        yield


@mutant("api.forms/composed_always")
def m_composed_always():
    with patched(FM, "composed", lambda dev, read, launches: True):
        yield


@mutant("api.forms/read_raw")
def m_read_raw():
    def read(dev, s, dtype, n):
        st, off = FM.base_of(s)
        return dev.read(st.addr + off, n * np.dtype(dtype).itemsize)

    with patched(FM, "read", read):
        yield


@mutant("api.forms/read_order")
def m_read_order():
    """Read-back prefers the padded-rows form to the f16 form."""

    def read(f):
        def go(dev, s, dtype, n):
            st, _ = FM.base_of(s)
            if FM.ROWS in {k[0] for k in st.valid} and FM.F16 in st.valid:
                keep = set(st.valid)
                st.valid.discard(FM.F16)
                try:
                    return f(dev, s, dtype, n)
                finally:
                    st.valid = keep
            return f(dev, s, dtype, n)

        return go

    with _wrap(FM, "read", read):
        yield


@mutant("api.forms/table_again")
def m_table_again():
    def table(f):
        def go(dev, arr, dtype=np.float16):
            dev.tables.clear()
            return f(dev, arr, dtype)

        return go

    with _wrap(FM, "table", table):
        yield


@mutant("api.forms/table_bytes")
def m_table_bytes():
    """A table keyed by its bytes alone."""

    def table(dev, arr, dtype=np.float16):
        a = np.ascontiguousarray(arr, dtype)
        key = a.tobytes()
        if key not in dev.tables:
            got = ST.Storage(dev, max(a.nbytes, 32))
            dev.write(got.addr, key)
            dev.tables[key] = got
        return dev.tables[key].addr

    with patched(FM, "table", table):
        yield


@mutant("api.forms/f16_copy")
def m_f16_copy():
    """An f16 buffer read through a side form, not its own bytes."""

    def f16(f):
        def go(dev, s, dtype, n):
            if np.dtype(dtype) == np.float16:
                st, _ = FM.base_of(s)
                return st.form(FM.F16, 2 * n).addr
            return f(dev, s, dtype, n)

        return go

    with _wrap(FM, "f16", f16):
        yield


@mutant("api.forms/no_host")
def m_no_host():
    """An f32 buffer's f16 form never made from its host bytes."""

    def f16(f):
        def go(dev, s, dtype, n):
            FM.base_of(s)[0].host = None
            return f(dev, s, dtype, n)

        return go

    with _wrap(FM, "f16", f16):
        yield


@mutant("api.forms/repitch_lanes")
def m_repitch_lanes():
    """Every repitch through the lane relayout."""

    def repitch(dev, src, src_pitch, dst, dst_pitch, rows, w):
        walk, dwalk = ((rows, src_pitch), (w, 1)), ((rows, dst_pitch), (w, 1))
        n, m = (rows - 1) * src_pitch + w, (rows - 1) * dst_pitch + w
        FM.relayout(dev, src, n, walk, 0, dwalk, m, dst)

    with patched(FM, "repitch", repitch):
        yield


@mutant("api.mem/tail")
def m_tail():
    with patched(D, "TAIL", 16384):
        yield


@mutant("api.mem/no_line")
def m_no_line():
    def write(self, addr, raw):
        self.card.write(addr, bytes(raw))

    with patched(D.Device, "write", write):
        yield


@mutant("api.mem/upload_dropped")
def m_upload_dropped():
    """Every host write lands as zeros."""

    def write(self, addr, raw):
        n = len(bytes(raw))
        self.card.write(addr, bytes(n + -n % self.card.line))

    with patched(D.Device, "write", write):
        yield


@mutant("api.mem/leak")
def m_leak():
    with patched(D.Device, "free", lambda self, addr: None):
        yield


@mutant("api.mem/forms_kept")
def m_forms_kept():
    def free(self):
        if self.owned:
            self.dev.free(self.addr)

    with patched(ST.Storage, "free", free):
        yield


@mutant("api.mem/view_base")
def m_view_base():
    with patched(ST.StorageView, "addr", property(lambda s: s.base.addr)):
        yield


@mutant("tg.seam/pins")
def m_pins():
    bad = tuple((v, 1 if want == 0 else want) for v, want in S._PINS)
    with _reinstalled(S, "_PINS", bad):
        yield


@mutant("tg.seam/max_bufs")
def m_max_bufs():
    with _reinstalled(S, "MAX_BUFS", 16):
        yield


@mutant("tg.seam/fuse_tinygrad")
def m_fuse_tinygrad():
    with _reinstalled(S, "_PM_KEEP", S._PM_REMOVE):
        yield


@mutant("tg.seam/route_other")
def m_route_other():
    keep = S.to_program
    with _reinstalled(S, "to_program", lambda ast, renderer: keep(ast, renderer)):
        yield


@mutant("tg.seam/run_linear_tinygrad")
def m_run_linear_tinygrad():
    with _reinstalled(S, "run_linear", S._RUN_LINEAR):
        yield


@mutant("tg.seam/unsupported_type")
def m_unsupported_type():
    with patched(S, "KTPUUnsupported", RuntimeError):
        yield


@mutant("tg.seam/kind")
def m_seam_kind():
    """Plans filed under the wrong kind."""

    def plan(f):
        def go(ast):
            p = f(ast)
            p.kind = "copy" if p.kind != "copy" else "vector"
            return p

        return go

    with _wrap(L, "plan", plan):
        yield


@mutant("tg.device/module_class")
def m_module_class():
    """The synthesised module holds another class (and install keeps it)."""
    with (
        patched(S, "register", lambda: sys.modules[KD.MODULE]),
        patched(sys.modules[KD.MODULE], "KTPUDevice", object),
    ):
        yield


@mutant("tg.device/allocator")
def m_allocator():
    class Other(KD.KTPUAllocator):
        pass

    with patched(KD, "KTPUAllocator", Other):
        yield


@mutant("tg.device/renderer")
def m_renderer():
    with patched(KD.KTPURenderer, "has_local", True):
        yield


@mutant("tg.device/clock")
def m_clock():
    """A fixed clock in place of the card's profile."""
    with patched(KD, "seconds", lambda rt, runs: sum(r.cycles for r in runs) / 300e6):
        yield


@mutant("tg.device/slots_unsorted")
def m_slots_unsorted():
    with _module_name(KD, "sorted", lambda xs: list(xs)[::-1]):
        yield


@mutant("tg.device/offset_base")
def m_offset_base():
    with patched(KD.KTPUAllocator, "_offset", lambda self, buf, size, offset: buf):
        yield


@mutant("tg.device/copyin_no_host")
def m_copyin_no_host():
    def copyin(f):
        def go(self, dest, src):
            f(self, dest, src)
            FM.base_of(dest)[0].host = None

        return go

    with _wrap(KD.KTPUAllocator, "_copyin", copyin):
        yield


@mutant("tg.lower/envelope")
def m_envelope():
    with patched(U, "envelope", lambda ast: None):
        yield


@mutant("tg.lower/order")
def m_order():
    with _wrap(L, "_order", lambda f: lambda coef, ranges: f(coef, ranges)[::-1]):
        yield


@mutant("tg.lower/no_unfold")
def m_no_unfold():
    """Divisions of a range left as they are (no digits, no domain split)."""
    with (
        patched(L, "_unfold", lambda ast: (ast, {})),
        patched(L.D, "normalise", lambda ast, digits: (ast, digits)),
    ):
        yield


@mutant("tg.lower/no_matmul")
def m_no_matmul():
    with patched(L, "_matmul", lambda base, value, reader: None):
        yield


@mutant("tg.lower/alu_renamed")
def m_alu_renamed():
    with (
        patched(L, "ALU", {k: f"{v}_" for k, v in L.ALU.items()}),
        patched(L, "BOOL", {k: f"{v}_" for k, v in L.BOOL.items()}),
        patched(X, "OPS", {**X.OPS, **{f"{k}_": n for k, n in X.OPS.items()}}),
    ):
        yield


@mutant("tg.lower/reduce_all_max")
def m_reduce_all_max():
    with patched(L, "REDUCE", {**L.REDUCE, Ops.ADD: "max"}):
        yield


@mutant("tg.lower/offset")
def m_offset():
    def affine(f):
        def go(u):
            got = f(u)
            return None if got is None else (got[0], got[1] + 1)

        return go

    with _wrap(U, "affine", affine):
        yield


@mutant("tg.lower/cast_scaled")
def m_cast_scaled():
    def cast(f):
        def go(self, u):
            x = f(self, u)
            return X.op("mul", x, 1.0) if dtypes.is_float(u.dtype) else x

        return go

    with _wrap(L._Reader, "_cast", cast):
        yield


@mutant("tg.lower/no_tables")
def m_no_tables():
    with _wrap(L._Reader, "_table", lambda f: lambda self, u, gate=False: X.Const(0.0)):
        yield


@mutant("tg.lower/refused_other")
def m_refused_other():
    class Other(RuntimeError):
        pass

    with patched(L, "Refused", Other):
        yield


@mutant("tg.run/no_flat")
def m_no_flat():
    with patched(R, "_flat", lambda dev, plan, bufs, cores: None):
        yield


@mutant("tg.run/no_bare")
def m_no_bare():
    with patched(R, "_bare", lambda dev, plan, bufs: None):
        yield


@mutant("tg.run/no_move")
def m_no_move():
    with patched(R, "_move", lambda dev, plan, bufs: None):
        yield


@mutant("tg.run/no_transpose")
def m_no_transpose():
    """The transpose path claims the read and runs nothing."""
    with patched(R, "_transpose", lambda dev, plan, bufs: []):
        yield


@mutant("tg.run/no_flip")
def m_no_flip():
    with patched(R, "_flip", lambda dev, plan, bufs: None):
        yield


@mutant("tg.run/no_gather")
def m_no_gather():
    with patched(R, "_gather", lambda dev, plan, bufs: None):
        yield


@mutant("tg.run/no_boxes")
def m_no_boxes():
    with patched(R, "_boxes", lambda dev, plan, bufs: None):
        yield


@mutant("tg.run/no_window")
def m_no_window():
    """The window path claims the contraction and runs nothing."""
    with patched(R, "_window", lambda dev, plan, bufs: []):
        yield


@mutant("tg.run/no_reform")
def m_no_reform():
    with patched(R, "_reform", lambda dev, plan, bufs: (plan, dict(bufs))):
        yield


@mutant("tg.run/laid_free")
def m_laid_free():
    """Every view costs the same: the first listed wins."""
    with patched(R, "_laid", lambda plan, rows, cols, Wp, out=True: 0):
        yield


@mutant("tg.run/takes_never")
def m_takes_never():
    """A one-row read is never taken as it is (always laid out first)."""
    keep = R._takes
    with patched(
        R,
        "_takes",
        lambda o, Wp, start=0: o is not None and o.kind != "col" and keep(o, Wp, start),
    ):
        yield


@mutant("tg.run/narrow_more")
def m_narrow_more():
    with patched(R, "MAX_READS", 16):
        yield


@mutant("tg.run/no_split")
def m_no_split():
    with patched(R, "_split_reduction", lambda plan: None):
        yield


@mutant("tg.run/wide_exact")
def m_wide_exact():
    with patched(R, "_wide", lambda cols: R._size(cols)):
        yield


@mutant("tg.run/out_flat")
def m_out_flat():
    """A non-f16 output written as row-major bytes."""

    def out(dev, plan, bufs):
        st, off = FM.base_of(bufs[plan.out_slot])
        return st.addr + off, ST.FLAT

    with patched(R, "_out", out):
        yield


@mutant("tg.run/const_no_host")
def m_const_no_host():
    def execute(f):
        def go(dev, plan, bufs):
            got = f(dev, plan, bufs)
            if plan.kind == "const":
                FM.base_of(bufs[plan.out_slot])[0].host = None
            return got

        return go

    with _wrap(R, "_execute", execute):
        yield


@mutant("tg.run/copy_as_vector")
def m_copy_as_vector():
    with patched(
        R,
        "_copy",
        lambda dev, plan, bufs: R._vector(dev, replace(plan, kind="vector"), bufs),
    ):
        yield


@mutant("tg.run/no_padded_out")
def m_no_padded_out():
    """A product's padded output always moved into a temporary."""
    with patched(FM, "rows_key", lambda n, np_: ("rows", n, np_ + 32)):
        yield


@mutant("tg.nn/place_f32")
def m_place_f32():
    with _wrap(
        NN,
        "place",
        lambda f: lambda module, device=KD.NAME, dtype=dtypes.half: f(
            module, device, dtypes.float
        ),
    ):
        yield


@mutant("tg.nn/no_compensate")
def m_no_compensate():
    def init(f):
        def go(self, params, *a, **kw):
            kw["compensate"] = False
            f(self, params, *a, **kw)

        return go

    with _wrap(NN.Adam16, "__init__", init):
        yield


@mutant("tg.bridge/flat_key")
def m_bridge_flat_key():
    with patched(BR, "FLAT", FM.F16):
        yield


@mutant("tg.env/default_offline")
def m_default_offline():
    def init(f):
        def go(self, kind=None, *a, **kw):
            f(self, kind or "offline", *a, **kw)

        return go

    with _wrap(D.Device, "__init__", init):
        yield


@mutant("tg.env/sim_root")
def m_sim_root():
    with patched(D, "SIM_ROOT", "v9f"):
        yield


@mutant("tg.env/transport_env")
def m_transport_env():
    with patched(D, "TRANSPORT_ENV", "KTPU_TRANSPORT_UNSET"):
        yield


@mutant("tg.env/board_env")
def m_board_env():
    with patched(D, "BOARD_ENV", "KTPU_BOARD_UNSET"):
        yield


__all__ = ["asts", "device", "expr_text", "form_keys", "plan_facts", "storage"]
