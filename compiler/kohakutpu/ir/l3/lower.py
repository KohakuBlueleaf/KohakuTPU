"""KohakuTPU's L3 -> L2 compiler (docs/projects/kohakutpu/ir/l3.md §5).

An L3 program at concrete shapes becomes an L2 `Schedule` over buffers a
target allocates, and the host's side: each input's packing, each output's
read-back. A map body (or the top level) is one group: vector ops only (one
`vgen` program over the whole tensors, split across the cores), or one `mmt` /
`conv3x3` then vector ops over its tile (cluster items, then each tile's
epilogue in place on a core). Anything else is refused with what was found.
"""

from dataclasses import dataclass, field

import numpy as np
from kohakuaccel.ir.l2 import Schedule
from kohakuaccel.ir.l3.instance import CarryInit, Loop, Put, Stmt, Update, instantiate
from kohakutpu.hw.mxfp7 import KBLOCK
from kohakutpu.ir import l2
from kohakutpu.ir.l1.kernels import attention as AT
from kohakutpu.ir.l1.kernels import conv2d as CV
from kohakutpu.ir.l1.kernels import stream
from kohakutpu.ir.l1.kernels import vgen as VG
from kohakutpu.ir.l1.kernels import vrun as VR
from kohakutpu.ir.l2.layouts import (
    BandLane,
    BiasB,
    ConvB,
    ConvTiles,
    Flat,
    MxA,
    MxB,
    OnesA,
    Rows,
    TileCols,
    Tiles,
)
from kohakutpu.ir.l2.lowerers import stream_cost

CLUSTER = ("mmt", "conv3x3")
#: Words a RUN at most: a VFILL/VDRAIN walk past 256 entries faults F_LEN.
MAX_WORDS = 256
#: A cluster tile when the program names none, sub-tiles a side.
GM, GN = 8, 8
#: `conv_tile`'s rows of sub-tiles (`l2.ops.conv2d`'s measured choice).
CONV_GM = 12


class LowerError(ValueError):
    pass


@dataclass
class Compiled:
    """A schedule and its host side: `inputs` ``[(buffer, pack(arrays))]``,
    `outputs` ``{name: unpack(get)}``; `origin` maps each item to the L3
    values it computes."""

    schedule: Schedule
    inputs: list = field(default_factory=list)
    outputs: dict = field(default_factory=dict)
    origin: dict = field(default_factory=dict)

    def run(self, target, arrays: dict) -> dict:
        for buf, pack in self.inputs:
            target.write(buf.base, bytes(pack(arrays)))
        for prog in l2.compile(self.schedule):
            target.run(prog)
        return {n: f(target.get) for n, f in self.outputs.items()}


def compile(
    module,
    program: str,
    shapes: dict,
    target,
    contract=True,
    bias="cluster",
    conv_tile=None,
    installs=1.0,
) -> Compiled:
    """`program` at the inputs' `shapes`, over memory `target` allocates
    (``alloc(nbytes)``, ``machine``). `contract`: a mul read once by an add
    becomes one fma (`vgen.contract`). `bias`: ``"cluster"`` adds a column
    vector read once, right after `mmt`, through the clusters' bias K-block
    (`matmul.bias_block`: past fp16, no cycles beyond the one sweep);
    ``"core"`` adds it in the vector epilogue.
    `conv_tile`: ``(gm, gn)`` sub-tiles of a `conv3x3` tile (L3 names none);
    None: `CONV_GM` and the widest gn of 8, 4, 2, 1 the channels take.
    `installs`: vector images installed an execution, weighing their install
    against their RUNs (1: the cores run other images between executions;
    0: they hold this program's)."""
    if bias not in ("core", "cluster"):
        raise LowerError(f"bias {bias!r}: 'core' or 'cluster'")
    if installs < 0:
        raise LowerError(f"installs {installs}: 0 or more")
    inst = instantiate(module, program, shapes)
    return _Lower(inst, target, contract, bias, conv_tile, installs).run()


# ----------------------------------------------------------------- analysis
def _key(op, inst, loops) -> tuple | None:
    """A tensor operand as dims of its tensor: a loop variable whose domain
    covers that dim, or ``"*"`` for whole; None when it is any other part."""
    shape = inst.params.get(op.name, inst.outputs.get(op.name))[1]
    entries = op.entries + (("full",),) * (len(shape) - len(op.entries))
    out = []
    for d, e in zip(shape, entries, strict=True):
        match e:
            case ("full",):
                out.append("*")
            case ("var", v):
                kind, a, b = loops[v]
                if (kind == "tiles" and a != d) or (
                    kind == "span" and (a, b) != (0, d)
                ):
                    return None
                out.append(v)
            case _:
                return None
    return tuple(out)


class _Lower:
    def __init__(
        self,
        inst,
        target,
        contract=True,
        bias="cluster",
        conv_tile=None,
        installs=1.0,
    ) -> None:
        self.inst, self.t, self.contract, self.bias = inst, target, contract, bias
        self.conv_tile, self.installs = conv_tile, float(installs)
        self.s = Schedule(machine=target.machine)
        self.out = Compiled(self.s)
        #: the L3 values the items added now compute (`Compiled.origin`)
        self.here: tuple = ()
        add = self.s.add

        def traced(*a, **k):
            i = add(*a, **k)
            self.out.origin[i] = self.here
            return i

        self.s.add = traced
        self.mgs = sorted(target.machine.units["MG"])
        self.vcs = sorted(target.machine.units["VC"])
        self.sinks: list = []
        #: (tensor, side, g, nk) -> the buffer it is packed into, once
        self.packed: dict = {}
        self.ix = None

    def index_words(self):
        """`attention.index_words`, once: the lane-group predicates' source."""
        if self.ix is None:
            self.ix = self.buf("index_words", Flat(32))
            self.out.inputs.append((self.ix, lambda a: AT.index_words().tobytes()))
        return self.ix

    def buf(self, name, layout, nbytes=None):
        n = layout.nbytes if nbytes is None else nbytes
        return self.s.buffer(name, n, layout, base=self.t.alloc(n))

    def run(self) -> Compiled:
        top = []
        for s in self.inst.body:
            if isinstance(s, Loop):
                if s.kind != "map":
                    raise LowerError("a top-level scan is not lowered")
                self.group(s.vars, s.body)
            else:
                top.append(s)
        if top:
            self.group((), tuple(top))
        missing = set(self.inst.outputs) - set(self.out.outputs)
        if missing:
            raise LowerError(f"outputs never stored: {sorted(missing)}")
        return self.out

    # ------------------------------------------------------------- a group
    def group(self, vars, body) -> None:
        loops = {v: (k, a, b) for v, k, a, b in vars}
        if any(isinstance(s, Loop) and s.kind == "scan" for s in body):
            Scan(self, loops, body).lower()
            return
        if any(isinstance(s, Loop) for s in body):
            kinds = sorted({s.kind for s in body if isinstance(s, Loop)})
            raise LowerError(f"a {'/'.join(kinds)} inside a map is not lowered")
        puts = [s for s in body if isinstance(s, Put)]
        stmts = [s for s in body if isinstance(s, Stmt)]
        if len(puts) != 1:
            raise LowerError(f"a group stores {len(puts)} values; one is lowered")
        (put,) = puts
        if put.value.name is None or put.value.tensor or put.value.entries:
            raise LowerError("a stored value is a whole value of the group")
        if put.target.name in self.out.outputs:
            raise LowerError(f"{put.target.name} is stored by two groups")
        if any(o.tensor and o.name in self.inst.outputs for s in stmts for o in s.args):
            raise LowerError("a group reading another group's output is not lowered")
        cl = [s for s in stmts if s.op in CLUSTER]
        other = [s for s in stmts if s.op not in CLUSTER]
        bad = sorted(
            {
                s.op
                for s in other
                if s.op in ("quantise", "transpose", "gather", "scatter")
            }
        )
        if bad:
            raise LowerError(
                f"{', '.join(bad)} outside attention's scan is not lowered"
            )
        self.here = tuple(s.name for s in stmts)
        if not cl:
            self.vector(loops, other, put)
        elif len(cl) == 1 and cl[0] is stmts[0]:
            self.cluster(loops, cl[0], other, put)
        else:
            raise LowerError("a group is one cluster op, then vector ops over its tile")

    # --------------------------------------------------- vector-only groups
    def vector(self, loops, stmts, put) -> None:
        inst = self.inst
        dtype, oshape = inst.outputs[put.target.name]
        okey = _key(put.target, inst, loops)
        if okey is None or set(loops) - set(okey):
            raise LowerError(f"the store to {put.target.name} is not whole tiles of it")
        if dtype != "f16":
            raise LowerError(f"{put.target.name} is {dtype}; the cores store fp16")
        reduces = any(s.op in VG.REDUCE for s in stmts)
        if reduces and okey[-1] != "*":
            raise LowerError("a reduction over part of a row is not lowered")
        ins, cols = [], []

        def classify(o):
            if o.name is None:
                return ("s", o.value)
            if not o.tensor:
                if any(e not in (("full",), ("new",)) for e in o.entries):
                    raise LowerError(f"{o.name} indexed inside the group")
                return ("v", o.name)
            tdt, tshape = inst.params[o.name]
            if tdt != "f16":
                raise LowerError(f"{o.name} is {tdt}; the cores stream fp16")
            k = _key(o, inst, loops)
            if k == okey and tshape == oshape:
                if o.name not in ins:
                    ins.append(o.name)
                return ("in", ins.index(o.name))
            if len(tshape) == 1 and tshape[0] == oshape[-1] and k == ("*",):
                if okey[-1] != "*":
                    raise LowerError(f"{o.name}: a column vector over column tiles")
                if o.name not in cols:
                    cols.append(o.name)
                return ("col", cols.index(o.name))
            raise LowerError(
                f"{o.name}{list(o.entries)} is neither the stored tile nor a "
                "column vector"
            )

        nodes = self.nodes(stmts, classify, {put.value.name})
        rows_mode = reduces or bool(cols)
        n = int(np.prod(oshape))
        c = oshape[-1]
        geom = (c,) if rows_mode else (0,)
        spec = (
            "vp",
            "rows" if rows_mode else "flat",
            geom,
            len(ins),
            len(cols),
            nodes,
            put.value.name,
        )
        try:
            slots = VG.slots(spec)
            resident = VG.resident_words(spec, 0)
        except VG.VgenError as e:
            raise LowerError(str(e)) from e
        if c % 16:
            raise LowerError(f"rows of {c} are not whole 16-element words")
        if rows_mode:
            nrows = n // c
            w = c // 16
            cores, per = self.split(nrows, VG.ROWS)
            words, share = self.run_words(spec, per * w, VG.ROWS * w, slots, resident)
            runs, step = per * w // words, words * 32
            layout = Rows(n, c)
            span = per * c * 2
        else:
            cores, per = self.split(n, 16 * VG.SLICE)
            words, share = self.run_words(spec, per // 16, VG.SLICE, slots, 0)
            runs, step = per // (16 * words), words * 32
            layout = Flat(n)
            span = per * 2
        srcs = []
        for name in ins:
            b = self.buf(name, layout)
            self.out.inputs.append(
                (b, lambda a, name=name, lay=layout: lay.pack(np.ravel(a[name])))
            )
            srcs.append(b)
        dst = self.buf(put.target.name, layout)
        self.out.outputs[put.target.name] = (
            lambda get, b=dst, sh=oshape: np.frombuffer(
                get(b.base, b.nbytes), np.float16
            )
            .astype(np.float64)
            .reshape(sh)
        )
        res = None
        if cols:
            res = self.buf(f"{put.target.name}.cols", Flat(len(cols) * c))
            self.out.inputs.append(
                (
                    res,
                    lambda a, cols=tuple(cols): np.concatenate(
                        [np.ravel(a[k]) for k in cols]
                    )
                    .astype(np.float16)
                    .tobytes(),
                )
            )
        for k, core in enumerate(cores):
            off = k * span
            params = {
                "body": spec,
                "words": words,
                "runs": runs,
                "install": share,
                "step": step,
                "srcs": [b.base + off for b in srcs],
                "dst": dst.base + off,
            }
            reads = [b.view(off, span) for b in srcs]
            if res is not None:
                params["resident_at"] = res.base
                reads.append(res.view())
            self.s.add(
                "vec_stream",
                "VC",
                params,
                reads=reads,
                writes=[dst.view(off, span)],
                at=core,
            )

    def nodes(self, stmts, classify, keep, kind_of=None) -> tuple:
        out = tuple(self.node(s, classify) for s in stmts)
        return VG.contract(out, keep, kind_of) if self.contract else out

    def node(self, s: Stmt, classify) -> tuple:
        attrs = dict(s.attrs)
        if s.op in VG.REDUCE:
            arg_shape = self.shape(s.args[0])
            axis = attrs.get("axis", len(arg_shape) - 1) % max(len(arg_shape), 1)
            if axis != len(arg_shape) - 1:
                raise LowerError(f"{s.name} = {s.op}: along rows only (the last axis)")
        if s.op == "fill":
            return (s.name, "copy", (classify(s.args[0]),))
        if s.dtype not in ("f16", "f32", "bool"):
            raise LowerError(f"{s.name} is {s.dtype}; the cores compute floats")
        return (s.name, s.op, tuple(classify(a) for a in s.args))

    def shape(self, o) -> tuple:
        if o.name is None:
            return ()
        table = (
            self.inst.values if not o.tensor else (self.inst.params | self.inst.outputs)
        )
        return table[o.name][1]

    def split(self, n: int, unit: int) -> tuple:
        """The most cores `n` splits across in whole `unit`s: ``(cores, per)``."""
        for k in range(len(self.vcs), 0, -1):
            if n % (k * unit) == 0:
                return self.vcs[:k], n // k
        raise LowerError(f"{n} is not whole {unit}s")

    def run_words(
        self, spec, total: int, unit: int, slots: int, resident: int, streams=1
    ) -> tuple:
        """``(words a RUN, install share)`` for `streams` streams of `total`
        words a core: words whole `unit`s dividing `total` that fit L1 and a
        walk, of those whose images fit instruction memory the one the
        modelled cost (`lowerers.stream_cost`) puts lowest, each stream paying
        its share of the core's `installs`."""
        share = self.installs / streams
        best = None
        for words in range(unit, min(total, MAX_WORDS) + 1, unit):
            if total % words or 2 * slots * words + resident > stream.L1_WORDS:
                continue
            try:
                cost = stream_cost(spec, words, total // words, share)
            except VG.VgenError:
                continue
            if best is None or cost <= best[0]:
                best = (cost, words)
        if best is None:
            raise LowerError(f"no RUN of {unit}-word steps fits L1 and the images")
        return best[1], share

    # ------------------------------------------------ cluster + epilogue
    def cluster(self, loops, c: Stmt, stmts, put) -> None:
        inst = self.inst
        tname = put.target.name
        dtype, oshape = inst.outputs[tname]
        if c.dtype != "f16":
            raise LowerError(f"{c.name} is {c.dtype}; a cluster drains fp16")
        if dtype != "f16":
            raise LowerError(f"{tname} is {dtype}; the cores store fp16")
        a, b = c.args
        for o in (a, b):
            if not o.tensor or o.name not in inst.params:
                raise LowerError(
                    f"{c.op} of {o.name}: the clusters read host-packed mx7 parameters here"
                )
        okey = _key(put.target, inst, loops)
        if okey is None or set(loops) - set(okey):
            raise LowerError(f"the store to {tname} is not whole tiles of it")
        peeled = self.cluster_bias(loops, c, stmts, okey) if c.op == "mmt" else None
        tile = {c.name}
        if peeled is not None:
            stmts = [s for s in stmts if s is not peeled[1]]
            tile.add(peeled[1].name)
        self.here = tuple(sorted(tile))
        if c.op == "mmt":
            geo = self.mmt(loops, c, okey, oshape, peeled and peeled[0], not stmts)
        else:
            if loops:
                raise LowerError("conv3x3 inside a map is not lowered")
            geo = self.conv(c, oshape)
        out_buf, items, layout, gn = geo
        self.out.outputs[tname] = lambda get, b=out_buf: b.layout.unpack(get, b.base)
        if not stmts:
            if put.value.name not in tile:
                raise LowerError("the stored value is not the group's")
            return
        if any(s.op in VG.REDUCE for s in stmts):
            raise LowerError("a row reduction over a cluster tile is not lowered")
        ins, cols = [c.name], []

        def classify(o):
            if o.name is None:
                return ("s", o.value)
            if not o.tensor:
                if any(e not in (("full",), ("new",)) for e in o.entries):
                    raise LowerError(f"{o.name} indexed inside the epilogue")
                if o.name in tile:
                    return ("in", 0)
                return ("v", o.name)
            tdt, tshape = inst.params[o.name]
            if tdt != "f16":
                raise LowerError(f"{o.name} is {tdt}; the cores stream fp16")
            k = _key(o, inst, loops)
            if k == okey and tshape == oshape:
                if o.name not in ins:
                    ins.append(o.name)
                return ("in", ins.index(o.name))
            if len(tshape) == 1 and tshape[0] == oshape[-1] and k == okey[-1:]:
                if o.name not in cols:
                    cols.append(o.name)
                return ("col", cols.index(o.name))
            raise LowerError(
                f"{o.name}{list(o.entries)} is neither the tile nor a column vector"
            )

        nodes = self.nodes(stmts, classify, {put.value.name})
        spec = ("vp", "flat", (gn,), len(ins), len(cols), nodes, put.value.name)
        try:
            slots = VG.slots(spec)
            resident = VG.resident_words(spec, 0)
            VG.col_walk(spec)
        except VG.VgenError as e:
            raise LowerError(str(e)) from e
        span_words = layout.gm * layout.gn
        unit = gn if gn % VG.SLICE == 0 else VG.SLICE
        streams = -(-len(items) // len(self.vcs))
        words, share = self.run_words(spec, span_words, unit, slots, resident, streams)
        extra = []
        for name in ins[1:]:
            eb = self.buf(name, out_buf.layout)
            self.out.inputs.append(
                (eb, lambda a, name=name, lay=out_buf.layout: lay.pack(a[name]))
            )
            extra.append(eb)
        res = None
        if cols:
            lay = TileCols(oshape[-1], gn, len(cols))
            res = self.buf(f"{tname}.cols", lay)
            self.out.inputs.append(
                (
                    res,
                    lambda a, cols=tuple(cols), lay=lay: lay.pack([a[k] for k in cols]),
                )
            )
        sinks = self.sink_bufs(words)
        self.here = tuple(s.name for s in stmts)
        for n, (t, j) in enumerate(items):
            (view,) = self.s.items[t].writes
            k = n % len(self.vcs)
            srcs = [view.address] + [e.base + view.offset for e in extra]
            params = {
                "body": spec,
                "words": words,
                "runs": span_words // words,
                "install": share,
                "step": words * 32,
                "srcs": srcs,
                "dst": view.address,
                "sink": sinks[k].base,
            }
            reads = [view] + [e.view(view.offset, view.nbytes) for e in extra]
            if res is not None:
                off, size = res.layout.tile(j)
                params["resident_at"] = res.base + off
                reads.append(res.view(off, size))
            self.s.add(
                "vec_stream",
                "VC",
                params,
                reads=reads,
                writes=[view, sinks[k].view()],
                at=self.vcs[k],
            )

    def sink_bufs(self, words: int) -> list:
        need = words * 32
        if not self.sinks or self.sinks[0].nbytes < need:
            self.sinks = [
                self.buf(f"sink{k}", Flat(need // 2)) for k in range(len(self.vcs))
            ]
        return self.sinks

    def cluster_bias(self, loops, c: Stmt, stmts, okey):
        """``(bias, add)`` for ``add c, bias[j]`` when `bias` is ``"cluster"``
        and that add is the only reader of `c`: the clusters' bias K-block
        adds it (`matmul.bias_block`); otherwise None."""
        if self.bias != "cluster":
            return None
        readers = [s for s in stmts if any(a.name == c.name for a in s.args)]
        if len(readers) != 1 or readers[0].op != "add":
            return None
        s = readers[0]
        names = [a.name for a in s.args]
        if names.count(c.name) != 1:
            return None
        o = s.args[1 - names.index(c.name)]
        if (
            o.name is None
            or not o.tensor
            or o.entries
            and _key(o, self.inst, loops) != okey[-1:]
        ):
            return None
        dt, shape = self.inst.params[o.name]
        return (o.name, s) if dt == "f16" and len(shape) == 1 else None

    def mmt(self, loops, c: Stmt, okey, oshape, bias=None, late=True) -> tuple:
        """`late`: hold each tile's fused drain behind the next tile's work
        (the clusters' best alone); a tile a vector core consumes drains at
        once (MEASURED, l2.md §2: 515.8k against 531.0k fused 1024^3)."""
        inst = self.inst
        a, b = c.args
        ka, kb = _key(a, inst, loops), _key(b, inst, loops)
        (m, kdim), (n, _) = inst.params[a.name][1], inst.params[b.name][1]
        if inst.params[a.name][0] != "mx7" or inst.params[b.name][0] != "mx7":
            raise LowerError("mmt reads mx7 parameters")
        if loops:
            if (
                ka is None
                or kb is None
                or ka[1] != "*"
                or kb[1] != "*"
                or okey != (ka[0], kb[0])
            ):
                raise LowerError("mmt in a map: y[i, j] = mmt x[i, :], w[j, :]")
            bm, bn = loops[ka[0]][2], loops[kb[0]][2]
        else:
            if a.entries or b.entries:
                raise LowerError("mmt outside a map reads whole operands")
            bm, bn = 4 * GM, 4 * GN
        if bm % 4 or bn % 4 or m % bm or n % bn:
            raise LowerError(f"a {bm}x{bn} tile does not tile {m}x{n} in 4x4 sub-tiles")
        gm, gn = bm // 4, bn // 4
        blocks = kdim // KBLOCK
        nk = 2 if blocks % 2 == 0 else 1
        la, lb, lc = MxA(m, kdim, gm, nk), MxB(n, kdim, gn, nk), Tiles(m, n, gm, gn)
        ab, bb = self.buf(a.name, la), self.buf(b.name, lb)
        self.out.inputs += [
            (ab, lambda x, k=a.name, l=la: l.pack(x[k])),
            (bb, lambda x, k=b.name, l=lb: l.pack(x[k])),
        ]
        cb = self.buf(f"{c.name}.tiles", lc)
        extra = {}
        if bias is not None:
            ones, bl = OnesA(gm), BiasB(n, gn)
            ob, biasb = self.buf("ones", ones), self.buf(bias, bl)
            self.out.inputs += [
                (ob, lambda x, l=ones: l.pack()),
                (biasb, lambda x, k=bias, l=bl: l.pack(x[k])),
            ]
            extra = {"ones": ob, "bias": biasb}
        items = l2.ops.matmul(self.s, self.mgs, ab, bb, cb, late=late, **extra)
        tn = lc.tn
        return cb, [(t, idx % tn) for idx, t in enumerate(items)], lc, gn

    def conv(self, c: Stmt, oshape) -> tuple:
        inst = self.inst
        x, w = c.args
        if x.entries or w.entries:
            raise LowerError("conv3x3 reads whole operands")
        h, wd, cin = inst.params[x.name][1]
        cout = inst.params[w.name][1][0]
        if self.conv_tile is not None:
            gm, gn = self.conv_tile
        else:
            gm = CONV_GM
            gn = next((g for g in (8, 4, 2, 1) if cout % (4 * g) == 0), None)
        if gn is None or cout % (4 * gn):
            raise LowerError(
                f"{cout} output channels are not whole tiles of {gn} groups"
            )
        cbc = CV.chunk_blocks(cin, gm, gn)
        lx, lw = BandLane(h, wd, cin, gm, cbc), ConvB(cout, cin, gn, cbc)
        xb, wb = self.buf(x.name, lx), self.buf(w.name, lw)
        self.out.inputs += [
            (xb, lambda a, k=x.name, l=lx: l.pack(a[k])),
            (wb, lambda a, k=w.name, l=lw: l.pack(np.transpose(a[k], (0, 3, 1, 2)))),
        ]
        lo = ConvTiles(h, wd, cout, gm, gn)
        cb = self.buf(f"{c.name}.tiles", lo)
        items = l2.ops.conv2d(self.s, self.mgs, xb, wb, _Alias(cb, lo.tiles))
        tn = lo.tiles.tn
        return cb, [(t, idx % tn) for idx, t in enumerate(items)], lo.tiles, gn


#: A carry's infinite start as the cores hold it: `vec_alu.v`'s exp2 of it is 0
#: (the hand-written attention's value).
INF = 60000.0


def _points(vars) -> list:
    """Every point of a map's domains: ``{var: index}`` (a tile's number)."""
    axes = [
        [(v, k) for k in (range(a, b) if kind == "span" else range(a // b))]
        for v, kind, a, b in vars
    ]
    out = [{}]
    for axis in axes:
        out = [p | {v: k} for p in out for v, k in axis]
    return out


class Scan:
    """A map body that carries values through a scan: per map point, on one
    cluster and one core, the scan body's phases in order -- each cluster op a
    `gemm`, each `quantise` a mover item, each run of vector ops between them a
    `vec_prog` RUN (`vrun`) over state the core keeps in its L1 -- then the
    carries' last values through the ops after the scan into the stored tile."""

    def __init__(self, low: "_Lower", loops: dict, body) -> None:
        self.low, self.inst, self.loops = low, low.inst, loops
        self.inits = [s for s in body if isinstance(s, CarryInit)]
        scans = [s for s in body if isinstance(s, Loop)]
        if len(scans) != 1 or scans[0].kind != "scan":
            raise LowerError("a scan group holds one scan")
        self.scan = scans[0]
        at = body.index(self.scan)
        if any(not isinstance(s, CarryInit) for s in body[:at]):
            raise LowerError("only carries come before a scan")
        self.post = [s for s in body[at + 1 :] if isinstance(s, Stmt)]
        puts = [s for s in body[at + 1 :] if isinstance(s, Put)]
        if len(puts) != 1:
            raise LowerError("a scan group stores one value")
        self.put = puts[0]
        ((self.jv, jkind, ext, self.bk),) = self.scan.vars
        if jkind != "tiles":
            raise LowerError("a scan over tiles is lowered")
        self.steps = ext // self.bk
        self.carries = {c.name: c for c in self.inits}

    # ------------------------------------------------------------- analysis
    def phases(self) -> list:
        """``[("unit", stmt) | ("vec", [stmts])]`` and the carries' updates."""
        out, cur = [], None
        self.updates = {}
        for s in self.scan.body:
            if isinstance(s, Update):
                self.updates[s.name] = s.value
                continue
            if not isinstance(s, Stmt):
                raise LowerError("a scan body is ops and carry updates")
            if s.op in CLUSTER + ("quantise",):
                out.append(("unit", s))
                cur = None
            else:
                if cur is None:
                    cur = ("vec", [])
                    out.append(cur)
                cur[1].append(s)
        for name, v in self.updates.items():
            if v.name is None or v.entries or v.name not in self.inst.values:
                raise LowerError(f"next {name}: a value of the scan body")
        return out

    def kind(self, name: str) -> tuple:
        """``("W", width)`` or ``("P", 0)`` of a value or carry."""
        shape = self.inst.values[name][1]
        if len(shape) == 1:
            return "P", 0
        if len(shape) == 2:
            return "W", shape[1] // 4
        raise LowerError(f"{name} is {shape}; a scan's values are tiles or rows")

    def lower(self) -> None:
        low = self.low
        phases = self.phases()
        made: dict = {}  # value -> phase index
        for k, (kind, x) in enumerate(phases):
            for s in [x] if kind == "unit" else x:
                made[s.name] = k
        readers: dict = {}
        for k, (kind, x) in enumerate(phases):
            for s in [x] if kind == "unit" else x:
                for a in s.args:
                    if a.name is not None and not a.tensor:
                        readers.setdefault(a.name, set()).add(k)
        bq = None
        for s in (x for kind, x in phases if kind == "unit"):
            rows = s.shape[0]
            if bq not in (None, rows):
                raise LowerError(f"the scan's tiles are {bq} and {rows} rows")
            bq = rows
        if bq is None or bq % 4 or not 1 <= bq // 4 <= 8:
            raise LowerError("a scan's cluster tiles are 4..32 rows")
        self.gm = gm = bq // 4
        for c in self.inits:
            if c.shape[0] != bq:
                raise LowerError(
                    f"carry {c.name} is {c.shape}; the tiles are {bq} rows"
                )
        # state: carries, then values crossing vector phases
        state, at = [], 0

        def slot(name, kind, width):
            nonlocal at
            state.append((name, kind, width, at))
            at += gm * max(width, 1)

        for c in self.inits:
            k, w = self.kind(c.name)
            slot(c.name, k, w)
        crossing = []
        for k, (kind, x) in enumerate(phases):
            if kind != "vec":
                continue
            for s in x:
                later = {r for r in readers.get(s.name, ()) if r > k}
                if any(phases[r][0] == "vec" for r in later):
                    kk, w = self.kind(s.name)
                    slot(s.name, kk, w)
                    crossing.append(s.name)
        for carry, v in self.updates.items():
            reads = [r for r in readers.get(carry, ()) if r > made.get(v.name, -1)]
            if reads:
                raise LowerError(f"{carry} is read after the phase that updates it")
        fills = [
            self.kind(s.name)[1]
            for kind, s in phases
            if kind == "unit" and s.op in CLUSTER
        ]
        wide = [
            self.kind(n)[1]
            for n in self.inst.values
            if len(self.inst.values[n][1]) == 2 and self.inst.values[n][1][0] == bq
        ]
        slot("$in", "$in", max(fills, default=1))
        slot("$out", "$out", max(wide, default=1))
        state.append(("$ix", "$ix", 0, at))
        at += 2
        if at > stream.L1_WORDS:
            raise LowerError(
                f"the scan's state takes {at} of L1's {stream.L1_WORDS} words"
            )
        put_value = self.put.value.name
        skind = {n: k for n, k, _, _ in state}

        def kind_of(a):
            return "S" if a[0] == "s" else "W" if a[0] == "in" else skind[a[1]]

        # runs
        runs = [
            (
                "init",
                0,
                (),
                tuple(("st", ("s", self.start(c)), c.name) for c in self.inits)
                + (("ix",),),
            )
        ]
        quant_of = {}
        for k, (kind, x) in enumerate(phases):
            if kind == "unit":
                continue
            fill_src = None

            def arg(a, k=k):
                nonlocal fill_src
                if a.name is None:
                    return ("s", a.value)
                if a.tensor:
                    raise LowerError(
                        f"{a.name}: a scan's vector ops read the scan's values"
                    )
                if any(e not in (("full",), ("new",)) for e in a.entries):
                    raise LowerError(f"{a.name} indexed inside the scan")
                if a.name in self.carries or a.name in crossing and made[a.name] < k:
                    return ("st", a.name)
                src = made.get(a.name)
                if src == k:
                    return ("v", a.name)
                if src is not None and phases[src][0] == "unit":
                    if fill_src not in (None, a.name):
                        raise LowerError("a vector phase reads two cluster results")
                    fill_src = a.name
                    return ("in",)
                raise LowerError(f"{a.name} is read before it is made")

            outs = []
            for s in x:
                later = {r for r in readers.get(s.name, ()) if r > k}
                if s.name in crossing:
                    outs.append(("st", ("v", s.name), s.name))
                for r in later:
                    if phases[r][0] == "unit":
                        if phases[r][1].op != "quantise":
                            raise LowerError(
                                f"{s.name} feeds {phases[r][1].op} unquantised"
                            )
                        outs.append(("drain", ("v", s.name), "quant"))
                        quant_of[phases[r][1].name] = (k, s.name)
            for carry, v in self.updates.items():
                if made.get(v.name) == k:
                    outs.append(("st", ("v", v.name), carry))
            keep = {o[1][1] for o in outs if o[1][0] == "v"}
            nodes = low.nodes(x, arg, keep, kind_of)
            fill = self.kind(fill_src)[1] if fill_src else 0
            runs.append((f"p{k}", fill, nodes, tuple(dict.fromkeys(outs))))
            phases[k] = ("vec", x, fill_src)
        missing = [c for c in self.carries if c not in self.updates]
        if missing:
            raise LowerError(f"carries never updated: {missing}")

        def post_arg(a):
            if a.name is None:
                return ("s", a.value)
            if a.name in self.carries:
                if a.entries and any(e not in (("full",), ("new",)) for e in a.entries):
                    raise LowerError(f"{a.name} indexed after the scan")
                return ("st", a.name)
            if any(s.name == a.name for s in self.post):
                return ("v", a.name)
            raise LowerError(f"{a.name}: after a scan, its carries and their ops")

        post = low.nodes(self.post, post_arg, {put_value}, kind_of)
        if put_value in self.carries:
            post, put_value = (("$o", "copy", (("st", put_value),)),), "$o"
        runs.append(("final", 0, post, (("drain", ("v", put_value), "tiles"),)))
        spec = ("vr", gm, tuple(state), tuple(runs))
        try:
            VR.images(spec)
        except VG.VgenError as e:
            raise LowerError(str(e)) from e
        self.emit(spec, phases, quant_of)

    def start(self, c) -> float:
        return max(-INF, min(INF, c.init))

    # ------------------------------------------------------------- emission
    def operand(self, o, which: str) -> tuple:
        """A cluster operand's layout and buffer: a tensor parameter packed
        whole, rows ``(lead..., rows)`` by K; ``(buffer, g, nk, row_axis,
        k_axis)`` with the axes' loop variables."""
        inst, low = self.inst, self.low
        dt, shape = inst.params[o.name]
        if dt != "mx7":
            raise LowerError(f"{o.name} is {dt}; the clusters read mx7")
        entries = o.entries + (("full",),) * (len(shape) - len(o.entries))
        lead, (re, ke) = entries[:-2], entries[-2:]
        for e in lead:
            if e[0] != "var" or self.loops.get(e[1], ("",))[0] != "span":
                raise LowerError(f"{o.name}: leading axes indexed by a map's points")
        rows, kdim = shape[-2], shape[-1]

        def size(e, d):
            if e == ("full",):
                return d, None
            if e[0] == "var":
                if e[1] == self.jv:
                    return self.bk, e[1]
                kind, _, b = self.loops[e[1]]
                if kind == "tiles":
                    return b, e[1]
            raise LowerError(f"{o.name}: a cluster operand's axis is whole or a tile")

        rt, rv = size(re, rows)
        kt, kv = size(ke, kdim)
        if rt % 4 or kt % KBLOCK:
            raise LowerError(
                f"{o.name}: {rt} rows by {kt} is not 4-row bands of 32-blocks"
            )
        g, nk = rt // 4, kt // KBLOCK
        lay = (MxA if which == "a" else MxB)(int(np.prod(shape[:-1])), kdim, g, nk)
        key = (o.name, which, g, nk)
        if key not in low.packed:
            b = low.buf(f"{o.name}.{which}", lay)
            low.out.inputs.append(
                (
                    b,
                    lambda x, k=o.name, l=lay, r=kdim: l.pack(
                        np.reshape(x[k], (-1, r))
                    ),
                )
            )
            low.packed[key] = b
        return low.packed[key], g, nk, lead, rv, kv, rows // rt

    def address(self, op, point, j) -> tuple:
        """``(addr, entries, keep)`` of a packed operand at a point and step."""
        b, _, _, lead, rv, kv, per = op
        flat = 0
        for e in lead:
            _, a, hi = self.loops[e[1]]
            flat = flat * (hi - a) + point[e[1]] - a
        row = point.get(rv, j if rv == self.jv else 0)
        chunk = j if kv == self.jv else 0
        off, entries = b.layout.chunk(flat * per + row, chunk)
        return b.base + off, entries, rv != self.jv and kv != self.jv

    def emit(self, spec, phases, quant_of) -> None:
        low, s = self.low, self.low.s
        gm = self.gm
        pairs = list(zip(low.mgs, low.vcs))
        ix = low.index_words()
        tname = self.put.target.name
        dtype, oshape = self.inst.outputs[tname]
        if dtype != "f16":
            raise LowerError(f"{tname} is {dtype}; the cores store fp16")
        width = self.inst.values[self.put.value.name][1][1] // 4
        okey = _key(self.put.target, self.inst, self.loops)
        if okey is None or okey[-1] != "*" or set(self.loops) - set(okey):
            raise LowerError(f"the store to {tname} is not whole tiles of it")
        rows = int(np.prod(oshape[:-1]))
        lo = Tiles(rows, oshape[-1], gm, width)
        out = low.buf(tname, lo)
        low.out.outputs[tname] = lambda get, b=out, sh=oshape: b.layout.unpack(
            get, b.base
        ).reshape(sh)
        ops = {}
        for k, ph in enumerate(phases):
            if ph[0] == "unit" and ph[1].op == "mmt":
                a, b = ph[1].args
                if a.tensor:
                    ops[(k, "a")] = self.operand(a, "a")
                ops[(k, "b")] = self.operand(b, "b") if b.tensor else None
                if ops[(k, "b")] is None:
                    raise LowerError(
                        f"{ph[1].name}: mmt's second operand is a parameter"
                    )
            elif ph[0] == "unit" and ph[1].op != "quantise":
                raise LowerError(f"{ph[1].op} inside a scan is not lowered")
        bufs = {}
        for p, (mg, vc) in enumerate(pairs):
            local = s.buffer(
                f"state_{vc[0]}_{vc[1]}",
                stream.L1_WORDS * 32,
                space=("local", tuple(vc)),
            )
            per = {}
            for k, ph in enumerate(phases):
                if ph[0] == "unit":
                    st = ph[1]
                    if st.op == "mmt":
                        w = st.shape[1] // 4
                        per[k] = low.buf(f"{st.name}.{p}", Flat(gm * w * 16))
                    else:
                        src_k, src = quant_of[st.name]
                        w = self.kind(src)[1]
                        per[("q16", k)] = low.buf(f"{src}.q16.{p}", Flat(gm * w * 16))
                        per[k] = low.buf(f"{st.name}.{p}", None, gm * (w // 8) * 128)
                        per[("drain", src_k)] = per[("q16", k)]
            bufs[p] = (local, per)
        for n, point in enumerate(
            _points(tuple((v, *self.loops[v]) for v in self.loops))
        ):
            p = n % len(pairs)
            mg, vc = pairs[p]
            local, per = bufs[p]
            state = local.view()

            def run(name, in_view=None, out_view=None, state=state, vc=vc):
                params = {"prog": spec, "run": name, "ix_at": ix.base}
                reads, writes = [state, ix.view()], [state]
                if in_view is not None:
                    params["in_at"] = in_view.address
                    reads.append(in_view)
                if out_view is not None:
                    params["out_at"] = out_view.address
                    writes.append(out_view)
                low.s.add("vec_prog", "VC", params, reads=reads, writes=writes, at=vc)

            low.here = tuple(c.name for c in self.inits)
            run("init")
            for j in range(self.steps):
                for k, ph in enumerate(phases):
                    low.here = tuple(
                        s.name for s in (ph[1] if ph[0] == "vec" else [ph[1]])
                    )
                    if ph[0] == "vec":
                        fill_src = ph[2]
                        in_view = (
                            per[made_k(phases, fill_src)].view() if fill_src else None
                        )
                        dv = per.get(("drain", k))
                        run(f"p{k}", in_view, dv.view() if dv is not None else None)
                        continue
                    st = ph[1]
                    if st.op == "quantise":
                        src, dst = per[("q16", k)], per[k]
                        s.add(
                            "quantise",
                            "mover",
                            {
                                "src": src.base,
                                "dst": dst.base,
                                "entries": dst.nbytes // 128,
                            },
                            reads=[src.view()],
                            writes=[dst.view()],
                        )
                        continue
                    a, b = st.args
                    if a.tensor:
                        aa = self.address(ops[(k, "a")], point, j)
                        areads = [
                            ops[(k, "a")][0].view(
                                aa[0] - ops[(k, "a")][0].base, aa[1] * 128
                            )
                        ]
                        nk = ops[(k, "a")][2]
                    else:
                        src = per[made_k(phases, a.name)]
                        nk = self.kind_q(a.name, phases)
                        aa = (src.base, gm * nk, False)
                        areads = [src.view()]
                    bb = self.address(ops[(k, "b")], point, j)
                    bbuf = ops[(k, "b")][0]
                    if ops[(k, "b")][2] != nk:
                        raise LowerError(
                            f"{st.name}: operands of {nk} and {ops[(k, 'b')][2]} K-blocks"
                        )
                    c = per[k]
                    s.add(
                        "gemm",
                        "MG",
                        {
                            "a": aa,
                            "b": bb,
                            "gm": gm,
                            "gn": st.shape[1] // 4,
                            "nk": nk,
                            "c_at": c.base,
                        },
                        reads=areads + [bbuf.view(bb[0] - bbuf.base, bb[1] * 128)],
                        writes=[c.view()],
                        at=mg,
                    )
            t = 0
            for v in okey[:-1]:
                kind, a, b = self.loops[v]
                t = (
                    t * ((b - a) if kind == "span" else a // b)
                    + point[v]
                    - (a if kind == "span" else 0)
                )
            off, size = lo.tile(t, 0)
            low.here = tuple(s.name for s in self.post)
            run("final", None, out.view(off, size))

    def kind_q(self, name, phases) -> int:
        """K-blocks of a quantised value: its columns over 32."""
        return self.inst.values[name][1][1] // KBLOCK


def made_k(phases, name) -> int:
    for k, ph in enumerate(phases):
        if ph[0] == "unit" and ph[1].name == name:
            return k
    raise LowerError(f"{name} is not a cluster or mover result")


class _Alias:
    """A buffer seen under another layout: `conv2d` reads `layout`, the items
    name the buffer itself."""

    def __init__(self, buf, layout) -> None:
        self._buf, self.layout = buf, layout

    def __getattr__(self, name):
        return getattr(self._buf, name)


__all__ = ["Compiled", "LowerError", "compile"]
