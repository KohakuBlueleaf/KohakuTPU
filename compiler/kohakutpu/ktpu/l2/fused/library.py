"""Library vector forms: L2 vector instances matched to hand-written
microkernel images, with the buffer formats those images use.

| form | instance | image (from) |
|---|---|---|
| softmax_quant | `st = load s[R rows, :]`, `m = reduce.max st`, `d = sub st, m[:, *]`, `pt = exp2 d`, `store l[..] <- reduce.sum pt`, `store p[..] <- quantise pt` | `attn_p` (attention.ktpu) |
| row_scale | `lt = load l[..]`, `ot = load o[.., :]`, `r = inv lt`, `store y[.., :] <- mul ot, r[:, *]` | `attn_out` (attention.ktpu) |

R = 256 rows, s 1024 columns (four 256 x 256 drained tiles), o and y 64
columns. `l` is held as the images hold it: one 32-byte word a 4-row
sub-row, f16[rows / 4, 16].
"""

import re

from kohakutpu.ktpu.l2 import nodes as L
from kohakutpu.ktpu.l2.emit import LowerError
from kohakutpu.ktpu.l2.fused.model import VecTask, region

from kohakutpu import ktpu

ROWS = 256
#: Image or node macro -> the kernel file holding it.
SOURCES = {"attn_p": "attention", "attn_out": "attention"}
SOURCES.update(
    {
        n: "flash"
        for n in (
            "flash_p",
            "flash_y",
            "flash_ones",
            "scores",
            "pv",
            "vload",
            "pvv",
            "after",
            "runp",
            "runx",
            "runh",
        )
    }
)


def _ops(s: L.Par) -> tuple:
    loads = [x for x in s.body if isinstance(x, L.Load)]
    ops = [x for x in s.body if isinstance(x, L.Op)]
    stores = [x for x in s.body if isinstance(x, L.Store)]
    return loads, ops, stores


def _stat(un, name: str, row0: int) -> tuple:
    """`name` as the images' per-sub-row words, and its offset for row0."""
    t = un.types[name]
    if len(t.shape) == 1:
        want = L.Type("f16", (t.shape[0] // 4, 16), "dram")
        un.types[name] = want
        un.layouts.types[name] = want
    elif t.shape[1] != 16 or t.dtype != "f16":
        raise LowerError(f"{name} is {t}, not a row statistic")
    return name, row0 // 4 * 32


def match(un, s: L.Par, env: dict, core: int) -> VecTask:
    loads, ops, stores = _ops(s)
    sig = (
        [x.op for x in ops],
        [x.value.op if isinstance(x.value, L.Op) else None for x in stores],
    )
    if sig == (["reduce.max", "sub", "exp2"], ["reduce.sum", "quantise"]):
        task = _softmax_quant(un, env, core, loads, ops, stores)
    elif sig == (["inv"], ["mul"]):
        task = _row_scale(un, env, core, loads, ops, stores)
    else:
        raise LowerError(f"no library form for the vector instance {sig}")
    elems = max(_count(r) for r in task.reads)
    task.work = elems * (len(ops) + len(stores) + len(loads))
    return task


def _count(reg) -> int:
    n = 1
    for lo, hi in reg[1]:
        n *= hi - lo
    return n


def _softmax_quant(un, env, core, loads, ops, stores) -> VecTask:
    (ld,) = loads
    sv = ld.src
    rs = region(sv, env, un.types[sv.name].shape)
    (r0, r1), (c0, c1) = rs[1]
    if r1 - r0 != ROWS or (c0, c1) != (0, 1024):
        raise LowerError("softmax_quant takes 256 rows of 1024 columns")
    lst, pst = stores
    lreg = region(lst.dst, env, (1 << 30,))
    preg = region(pst.dst, env, un.types[pst.dst.name].shape)
    task = VecTask(core, None, "attn_p")
    task.args = [
        "64",
        un.layouts.f16_tile(sv.name, r0, 0, 64, 64),
        "128K",
        un.layouts.mx7(pst.dst.name, r0, 0, 64),
        "16K",
        _stat(un, lst.dst.name, r0),
    ]
    task.reads = [rs]
    task.writes = [lreg, preg]
    return task


def _row_scale(un, env, core, loads, ops, stores) -> VecTask:
    by = {ld.name: ld for ld in loads}
    (inv,) = ops
    (st,) = stores
    lt = by[inv.args[0].name]
    ot = by[st.value.args[0].name]
    oreg = region(ot.src, env, un.types[ot.src.name].shape)
    (r0, r1), (c0, c1) = oreg[1]
    if r1 - r0 != ROWS or (c0, c1) != (0, 64):
        raise LowerError("row_scale takes 256 rows of 64 columns")
    # The statistic's region in L2 rows, before its type becomes the images'.
    lreg = region(lt.src, env, (1 << 30,))
    task = VecTask(core, None, "attn_out")
    task.args = [
        "8",
        un.layouts.f16_tile(ot.src.name, r0, 0, 64, 16),
        _stat(un, lt.src.name, r0),
        un.layouts.f16_tile(st.dst.name, r0, 0, 64, 16),
    ]
    task.reads = [oreg, lreg]
    task.writes = [region(st.dst, env, un.types[st.dst.name].shape)]
    return task


def text(images) -> str:
    """The source of `images` and every macro they expand, from their files."""
    out, done = [], set()
    for img in images:
        m = ktpu.load(ktpu.kernel(SOURCES[img]))
        todo = [img]
        while todo:
            n = todo.pop()
            if n in done:
                continue
            done.add(n)
            d = m.images.get(n) or m.macros[n]
            block = _block(m.source, d.line)
            out.append(block)
            todo += [x for x in re.findall(r"expand (\w+)\(", block) if x not in done]
    return "\n".join(out)


def _block(source: str, line: int) -> str:
    lines = source.splitlines()
    end = line
    while end < len(lines) and (not lines[end].strip() or lines[end][0] in " \t"):
        end += 1
    return "\n".join(lines[line - 1 : end]).rstrip() + "\n"


__all__ = ["SOURCES", "match", "text"]
