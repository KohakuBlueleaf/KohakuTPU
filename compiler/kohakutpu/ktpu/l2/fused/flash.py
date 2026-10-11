"""L2 -> L1 for the flash-attention body form (flash.ktpu's `flash.l2`): a
pattern lowering. The body is matched statement by statement against the
form -- scores of 128-query tiles on mg[2 (g % 2)], each 32-row tile's P
with a lazy per-half offset on core h, its P V partials on mg[1 + 2h], the
two halves combined on the cores -- and its sizes read off it; the L1 is the
form's node program for that many heads over the library's images and node
macros (`flash_p`, `flash_y`, `flash_ones`; `scores`, `pv`, `vload`, `pvv`,
`after`, `runp` from flash.ktpu).

What the form's L1 adds to its L2: P travels to the P V clusters' L1 by peer
drains (the L2's `pb`), O and l through staging rings of eight tiles (`o`,
`li`), S through a DRAM ring of four scores tiles, and I and the partials are
fetched one RUN ahead.
"""

from kohakutpu.ktpu.l2 import nodes as L
from kohakutpu.ktpu.l2.emit import LowerError, Text
from kohakutpu.ktpu.l2.fused import library

#: Images and node macros of the form.
NAMES = (
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
#: Op sequences of the form's two vector instances.
P_OPS = ["reduce.max", "add", "sub", "sub", "exp2"]
Y_OPS = ["max", "sub", "exp2", "sub", "exp2", "mul", "fma", "inv", "mul", "mul", "mul"]


def _vec_ops(par: L.Par) -> list:
    return [s.op for s in par.body if isinstance(s, L.Op)]


def match(body: L.Body) -> int | None:
    """The head count if `body` is the flash form, else None."""
    pars = [s for s in body.stmts if isinstance(s, L.Par)]
    if len(pars) != 2 or pars[1].unit != "mg":
        return None
    main = pars[1]
    fors = [s for s in main.body if isinstance(s, L.For)]
    if len(fors) != 2:
        return None
    inner = [s for s in fors[1].body if isinstance(s, L.Par)]
    if [p.unit for p in inner] != ["vc", "mg", "vc"]:
        return None
    if _vec_ops(inner[0]) != P_OPS or _vec_ops(inner[2]) != Y_OPS:
        return None
    if main.hi % 8:
        return None
    params = dict(body.params)
    q = params.get("q")
    if q is None or q.shape != (main.hi * 128, 64):
        raise LowerError("the flash form takes q of [128 g, 64]")
    return main.hi // 8


def lower(body: L.Body, heads: int, out: Text, node: Text) -> list:
    """Images and node macros into `out`, the node body into `node`; the L1
    parameters."""
    for line in library.text(NAMES).splitlines():
        out(line)
    out("")
    nt = 32 * heads
    rows = 1024 * heads
    node("send vc[0]")
    with node.block():
        node("run flash_ones()")
    node("wait vc[0]")
    node("expand vload(0)")
    node("for T in 0..4")
    with node.block():
        node("expand scores(T)")
    node("wait mg[0] upto=e{0}")
    node("for h in 0..2")
    with node.block():
        node("send vc[h]")
        with node.block():
            node(
                "run flash_p(s + h*128K, s + h*128K, 0, h, 1, 0, 0, 0, 0, 0, li + 7*1K + 512 + h*256)"
            )
    node(f"for t in 0..{nt + 1}")
    with node.block():
        node("when t > 0")
        with node.block():
            node("when t % 32 == 0")
            with node.block():
                node(f"expand after(t, {nt})")
        node(f"when t < {nt}")
        with node.block():
            node(f"expand runp(t, {nt})")
        node("when t % 32 > 0")
        with node.block():
            node(f"expand after(t, {nt})")
    node(f"for t in {nt - 5}..{nt}")
    with node.block():
        node("wait mg[1] upto=pa{t}")
        node("wait mg[3] upto=pb{t}")
        node("for h in 0..2")
        with node.block():
            node("send vc[h]")
            with node.block():
                node(
                    "run flash_y(h, o + 2*(t % 8)*4K + h*2K, li + (t % 8)*1K, y + t*4K + h*2K)"
                )
    return [
        ("q", L.Type("mx7a", (rows, 64), "dram", (("gm", 32), ("nk", 1)))),
        ("k", L.Type("mx7b", (rows, 64), "dram", (("gn", 128), ("nk", 1)))),
        ("v", L.Type("mx7b", (64, rows), "dram", (("gn", 16), ("nk", 16), ("t", 1)))),
        ("y", L.Type("f16", (rows, 64), "dram", (("tile", (8, 16)),))),
        ("s", L.Type("f16", (512, 1024), "dram")),
        ("o", L.Type("f16", (512, 64), "stage", (("tile", (8, 16)),))),
        ("li", L.Type("f16", (256, 32), "stage", (("tile", (8, 16)),))),
    ]


__all__ = ["lower", "match"]
