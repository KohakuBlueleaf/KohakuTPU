"""Numerical documentation and host API witnesses."""

import importlib
import re
import sys

import numpy as np
from harness.registry import mutant, patched, runner

from kohakutpu.language.numerics import fp16

from .target.l0 import ROOT

TM = importlib.import_module("kohakutpu.application.api.tensor")
DOC_BEFORE = False


@runner("num.api.dtype")
def api_dtype(inp, ctx):
    try:
        TM.tensor(np.zeros(4), dtype=np.dtype(inp["dtype"]))
    except TM.Unsupported:
        return {"refused": True}
    return {"refused": False}


@runner("num.doc")
def doc(inp, ctx):
    if DOC_BEFORE:
        text = inp["before"]
    else:
        lines = (ROOT / inp["path"]).read_text(encoding="utf-8").splitlines()
        a, b = inp.get("lines", [1, len(lines)])
        text = "\n".join(lines[a - 1 : b])
    return {p: bool(re.search(p, text, re.IGNORECASE)) for p in inp["has"]}


@mutant("num.fp16/no-saturation")
def no_saturation():
    with patched(fp16, "write", lambda x, **kw: np.asarray(x, np.float16)):
        yield


@mutant("num.api/int-tensor-accepted")
def int_tensor_accepted():
    with patched(TM, "tensor", lambda a, dtype=np.float16: None):
        yield


@mutant("num.doc/before")
def before():
    with patched(sys.modules[__name__], "DOC_BEFORE", True):
        yield
