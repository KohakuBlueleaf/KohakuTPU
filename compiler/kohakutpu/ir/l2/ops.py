"""Hand-written L2 schedules: what each L1 reference kernel decided, stated as
buffers, items and placement for the L2 -> L1 compiler. Every builder takes
buffers already given addresses and returns the items it added."""

from kohakutpu.ir.l1.kernels import attention as AT
from kohakutpu.ir.l1.kernels import conv2d as CV
from kohakutpu.ir.l1.kernels.matmul import ENTRY, LANES, SUBTILE
from kohakutpu.ir.l2.layouts import BandLane, ConvB, MxA, MxB, Tiles


def _check(cond: bool, why: str) -> None:
    if not cond:
        raise ValueError(why)


def matmul(s, mgs, a, b, c, late=True, ones=None, bias=None, package=0) -> list:
    """``c = a @ b.T`` (+ bias), one `gemm_tile` a C tile, round robin."""
    la, lb, lc = a.layout, b.layout, c.layout
    _check(
        isinstance(la, MxA) and isinstance(lb, MxB) and isinstance(lc, Tiles),
        "matmul wants MxA, MxB and Tiles buffers",
    )
    _check(
        la.k == lb.k and la.nk == lb.nk and (la.gm, lb.gm) == (lc.gm, lc.gn),
        "operand tilings disagree",
    )
    tm, tn = la.rows // (LANES * la.gm), lb.rows // (LANES * lb.gm)
    out = []
    for t in range(tm * tn):
        i, j = divmod(t, tn)
        aoff, asize = la.tile(i)
        boff, bsize = lb.tile(j)
        coff, csize = lc.tile(i, j)
        reads = [a.view(aoff, asize), b.view(boff, bsize)]
        params = {
            "a_at": a.base + aoff,
            "b_at": b.base + boff,
            "c_at": c.base + coff,
            "gm": la.gm,
            "gn": lb.gm,
            "nk": la.nk,
            "chunks": la.chunks,
            "late": late,
        }
        if bias is not None:
            boff_b = j * lb.gm * ENTRY
            reads += [ones.view(), bias.view(boff_b, lb.gm * ENTRY)]
            params |= {"ones_at": ones.base, "bias_at": bias.base + boff_b}
        out.append(
            s.add(
                "gemm_tile",
                "MG",
                params,
                reads=reads,
                writes=[c.view(coff, csize)],
                at=mgs[t % len(mgs)],
                package=package,
            )
        )
    return out


def conv2d(s, mgs, x, w, c, package=0) -> list:
    """3x3 same conv, one `conv_tile` a C tile, round robin."""
    lx, lw, lc = x.layout, w.layout, c.layout
    _check(
        isinstance(lx, BandLane) and isinstance(lw, ConvB) and isinstance(lc, Tiles),
        "conv2d wants BandLane, ConvB and Tiles buffers",
    )
    gm, gn, cbc = lx.gm, lw.gn, lx.cbc
    _, wp, positions = CV.geometry(lx.h, lx.w, gm)
    tm, tn = positions // gm, lw.cout // (LANES * gn)
    chunks = lx.c // (32 * cbc)
    out = []
    for t in range(tm * tn):
        i, j = divmod(t, tn)
        coff, csize = lc.tile(i, j)
        params = {
            "a_at": x.base,
            "a_chunk": lx.chunk_bytes,
            "row0": i * gm,
            "wp": wp,
            "b_at": w.base + j * lw.tile_bytes,
            "c_at": c.base + coff,
            "gm": gm,
            "gn": gn,
            "cbc": cbc,
            "chunks": chunks,
        }
        out.append(
            s.add(
                "conv_tile",
                "MG",
                params,
                reads=[x.view(), w.view(j * lw.tile_bytes, lw.tile_bytes)],
                writes=[c.view(coff, csize)],
                at=mgs[t % len(mgs)],
                package=package,
            )
        )
    return out


def stream(
    s, vcs, body, srcs, dst, n, words, sinks=None, resident=None, package=0
) -> list:
    """One `vec_stream` a core over an even split of `n` fp16 elements: inputs
    `srcs` (buffers), output `dst`, `words` L1 words a RUN."""
    per = n // len(vcs)
    batch = words * 16
    _check(
        n % len(vcs) == 0 and per % batch == 0,
        f"{per} elements a core is not whole {batch}-element RUNs",
    )
    out = []
    for k, core in enumerate(vcs):
        off = k * per * 2
        params = {
            "body": tuple(body),
            "words": words,
            "runs": per // batch,
            "step": batch * 2,
            "srcs": [b.base + off for b in srcs],
            "dst": dst.base + off,
        }
        reads = [b.view(off, per * 2) for b in srcs]
        writes = [dst.view(off, per * 2)]
        if sinks is not None:
            params["sink"] = sinks[k].base
            writes.append(sinks[k].view())
        if resident is not None:
            params["resident_at"] = resident.base
            reads.append(resident.view())
        out.append(
            s.add(
                "vec_stream",
                "VC",
                params,
                reads=reads,
                writes=writes,
                at=core,
                package=package,
            )
        )
    return out


def rows_stream(
    s, vcs, body, src, dst, n_rows, cols, rows, resident=None, package=0
) -> list:
    """`stream` for a row kernel: `rows` rows a RUN, the rows split by core."""
    w = cols // 16
    per = n_rows // len(vcs)
    _check(
        n_rows % len(vcs) == 0 and per % rows == 0 and rows % 8 == 0,
        f"{per} rows a core is not whole RUNs of {rows}",
    )
    out = []
    for k, core in enumerate(vcs):
        off = k * per * cols * 2
        params = {
            "body": tuple(body),
            "words": rows * w,
            "runs": per // rows,
            "step": rows * cols * 2,
            "srcs": [src.base + off],
            "dst": dst.base + off,
        }
        reads = [src.view(off, per * cols * 2)]
        if resident is not None:
            params["resident_at"] = resident.base
            reads.append(resident.view())
        out.append(
            s.add(
                "vec_stream",
                "VC",
                params,
                reads=reads,
                writes=[dst.view(off, per * cols * 2)],
                at=core,
                package=package,
            )
        )
    return out


def silu(s, vcs, x, y, n, words=256, group=3, sets=2, package=0) -> list:
    return stream(s, vcs, ("silu", group, sets), [x], y, n, words, package=package)


def binary(s, vcs, op, a, b, y, n, words=128, group=4, sets=2, package=0) -> list:
    return stream(
        s, vcs, ("binary", op, group, sets), [a, b], y, n, words, package=package
    )


def softmax(s, vcs, x, y, n_rows, cols, rows=8, block=8, package=0) -> list:
    return rows_stream(
        s,
        vcs,
        ("softmax", cols, rows, block),
        x,
        y,
        n_rows,
        cols,
        rows,
        package=package,
    )


def layernorm(s, vcs, x, y, gb, n_rows, cols, rows=8, eps=1e-5, package=0) -> list:
    return rows_stream(
        s,
        vcs,
        ("layernorm", cols, rows, eps),
        x,
        y,
        n_rows,
        cols,
        rows,
        resident=gb,
        package=package,
    )


def matmul_silu(
    s, mgs, vcs, a, b, c, sinks, late=False, words=256, group=3, sets=2, package=0
) -> list:
    """`matmul` tiles, then silu over each tile in place on the cores in turn:
    the compiler waits each tile's drain before its epilogue."""
    tiles = matmul(s, mgs, a, b, c, late=late, package=package)
    lc = c.layout
    span = lc.gm * lc.gn * SUBTILE
    _check(span % (words * 32) == 0, f"a tile is not whole {words}-word RUNs")
    out = list(tiles)
    for n, t in enumerate(tiles):
        (view,) = s.items[t].writes
        k = n % len(vcs)
        params = {
            "body": ("silu", group, sets),
            "words": words,
            "runs": span // (words * 32),
            "step": words * 32,
            "srcs": [view.address],
            "dst": view.address,
            "sink": sinks[k].base,
        }
        out.append(
            s.add(
                "vec_stream",
                "VC",
                params,
                reads=[view],
                writes=[view, sinks[k].view()],
                at=vcs[k],
                package=package,
            )
        )
    return out


def attention(
    s, mg, vc, q, k, v, o, scratch, idx, gm: int, blocks: int, package=0
) -> list:
    """One query block against `blocks` 64-key blocks (`kernels/attention.py`):
    S, softmax, quantise, P @ v, update per block; the core's local storage holds
    O and the row statistics between RUNs."""
    span = gm * AT.COLS * SUBTILE
    s_v, pv_v = scratch.view(0, span), scratch.view(span, span)
    p16_v, p7_v = scratch.view(2 * span, span), scratch.view(
        3 * span, gm * AT.NK * ENTRY
    )
    kv = AT.COLS * AT.NK * ENTRY
    local = s.buffer(f"attn@{vc}", 512 * 32, space=("local", tuple(vc)))
    state = local.view()
    run = {"gm": gm, "p16_at": p16_v.address, "o_at": o.base, "idx_at": idx.base}
    out = [
        s.add(
            "vec_run",
            "VC",
            run | {"run": "init"},
            reads=[idx.view()],
            writes=[state],
            at=vc,
            package=package,
        )
    ]
    for j in range(blocks):
        kj, vj = k.view(j * kv, kv), v.view(j * kv, kv)
        out.append(
            s.add(
                "gemm",
                "MG",
                {
                    "a": (q.base, gm * AT.NK, True),
                    "b": (kj.address, AT.COLS * AT.NK, False),
                    "gm": gm,
                    "gn": AT.COLS,
                    "nk": AT.NK,
                    "c_at": s_v.address,
                },
                reads=[q.view(), kj],
                writes=[s_v],
                at=mg,
                package=package,
            )
        )
        out.append(
            s.add(
                "vec_run",
                "VC",
                run | {"run": "softmax", "in_at": s_v.address},
                reads=[s_v, state],
                writes=[state, p16_v],
                at=vc,
                package=package,
            )
        )
        out.append(
            s.add(
                "quantise",
                "mover",
                {"src": p16_v.address, "dst": p7_v.address, "entries": gm * AT.NK},
                reads=[p16_v],
                writes=[p7_v],
                package=package,
            )
        )
        out.append(
            s.add(
                "gemm",
                "MG",
                {
                    "a": (p7_v.address, gm * AT.NK, False),
                    "b": (vj.address, AT.COLS * AT.NK, False),
                    "gm": gm,
                    "gn": AT.COLS,
                    "nk": AT.NK,
                    "c_at": pv_v.address,
                },
                reads=[p7_v, vj],
                writes=[pv_v],
                at=mg,
                package=package,
            )
        )
        out.append(
            s.add(
                "vec_run",
                "VC",
                run | {"run": "update", "in_at": pv_v.address},
                reads=[pv_v, state],
                writes=[state],
                at=vc,
                package=package,
            )
        )
    out.append(
        s.add(
            "vec_run",
            "VC",
            run | {"run": "final"},
            reads=[state],
            writes=[state, o.view()],
            at=vc,
            package=package,
        )
    )
    return out
