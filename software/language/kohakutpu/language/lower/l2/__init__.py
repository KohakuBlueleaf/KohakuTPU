"""L2 -> L1: an `fn NAME.l2` body to L1 `.ktpu` text (images and an
`fn NAME.l1` body), read back by the L1 readers like a hand kernel.

A body of one `par` on the vector cores is a stream (1-D tiles, `stream.py`)
or a row kernel (R x 128 tiles with row reductions, `rows.py`); a body with
cluster instances goes to `fused.py`.
"""

from kohakutpu.language.l2 import nodes as L
from kohakutpu.language.l2.writer import type_text
from kohakutpu.language.lower.l2 import fused
from kohakutpu.language.lower.l2.emit import LowerError
from kohakutpu.language.lower.l2.rows import Rows
from kohakutpu.language.lower.l2.stream import Stream
from kohakutpu.language.text.writer import Text


def lower(module, name: str, body: L.Body, **options) -> str:
    """The L1 text of `name.l2`: images, then `fn name.l1`. `options` go to
    the fused lowering (`lag=`)."""
    out = Text()
    node = Text(depth=1)
    out(f"target {module.target or 'ktpu.v9'}")
    out("")
    pars = [s for s in body.stmts if isinstance(s, L.Par)]
    params = list(body.params)
    if len(pars) == 1 and pars[0].unit == "vc" and len(pars) == len(body.stmts):
        par = pars[0]
        loops = [s for s in par.body if isinstance(s, L.For)]
        pre = [s for s in par.body if isinstance(s, L.Load)]
        if len(loops) != 1 or len(pre) + 1 != len(par.body):
            raise LowerError("a vector instance is loads, then one for loop")
        loop = loops[0]
        shape = next((s.type.shape for s in loop.body if isinstance(s, L.Load)), ())
        if len(shape) == 1 and not pre:
            Stream(name, par, loop).lower(out, node)
        elif len(shape) == 2:
            Rows(name, par, pre, loop).lower(out, node)
        else:
            raise LowerError(
                f"no lowering for a vector instance of tiles {list(shape)}"
            )
    else:
        params = fused.lower(module, name, body, out, node, **options)
    node("barrier")
    out(f"fn {name}.l1({', '.join(f'{n}: {type_text(t)}' for n, t in params)})")
    out.lines += node.lines
    return out.text()


__all__ = ["LowerError", "lower"]
