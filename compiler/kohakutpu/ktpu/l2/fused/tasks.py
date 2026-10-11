"""A fused L2 body unrolled into unit tasks with constant addresses.

Top-level statements: `buffer`s, then `par u in LO..HI on mg[EXPR]` phases.
A phase's instance body: gemm tiles (in `for` loops or not), each tile's
accumulator drained to memory (`store VIEW <- drain acc`) or to a value
(`h = drain acc`, a slot of an implicit scratch buffer), and vector
instances (`par c in 0..1 on vc[EXPR]`): an elementwise epilogue on drained
values, or a library form (`library.py`).
"""

from kohakutpu.ktpu.l2 import nodes as L
from kohakutpu.ktpu.l2.emit import LowerError
from kohakutpu.ktpu.l2.fused import library
from kohakutpu.ktpu.l2.fused.layout import NK, SUB, Layouts
from kohakutpu.ktpu.l2.fused.model import Tile, VecTask, overlaps, region
from kohakutpu.ktpu.l2.verify import value


class Unroll:
    def __init__(self, body: L.Body) -> None:
        self.types = {n: t for n, t in body.params}
        self.scratch: list = []
        for s in body.stmts:
            if isinstance(s, L.Buffer):
                self.types[s.name] = s.type
                self.scratch.append(s.name)
        self.layouts = Layouts(self.types)
        self.tiles: list = []
        self.vec: list = []
        #: Drained value name -> its slots in order of use.
        self.slots: dict = {}
        self.drained_shape: dict = {}
        #: Library images the body uses, by name.
        self.library: set = set()
        for s in body.stmts:
            if isinstance(s, L.Buffer):
                continue
            if not (isinstance(s, L.Par) and s.unit == "mg"):
                raise LowerError(
                    "a fused body is buffers, then `par ... on mg[...]` phases"
                )
            for u in range(s.lo, s.hi):
                env = {s.var: u}
                self.block(s.body, env, value(s.at, env), {})

    def block(self, stmts, env, unit, vals) -> None:
        for s in stmts:
            if isinstance(s, L.For):
                for i in range(s.lo, s.hi):
                    self.block(s.body, {**env, s.var: i}, unit, vals)
            elif isinstance(s, L.Op) and s.op == "gemm":
                vals[s.name] = self.gemm(s, env, unit)
            elif isinstance(s, L.Op) and s.op == "drain":
                tile = vals[s.args[0].name]
                slot = self.slot(s.name, s.type)
                self.finish(tile, slot)
                vals[s.name] = slot
            elif (
                isinstance(s, L.Store)
                and isinstance(s.value, L.Op)
                and s.value.op == "drain"
            ):
                tile = vals[s.value.args[0].name]
                t = self.types[s.dst.name]
                _, ((r0, _), (c0, _)) = region(s.dst, env, t.shape)
                out = self.layouts.f16_tile(s.dst.name, r0, c0, tile.gm, tile.gn)
                tile.writes.append(region(s.dst, env, t.shape))
                self.finish(tile, out)
            elif isinstance(s, L.Par) and s.unit == "vc":
                core = value(s.at, {**env, s.var: s.lo})
                self.vector(s, env, core, vals)
            else:
                raise LowerError(f"no fused lowering for {s!r}")

    def gemm(self, s: L.Op, env, unit) -> Tile:
        av, bv = s.args
        k = dict(s.attrs)["k"]
        ta, tb = self.types[av.name], self.types[bv.name]
        ra, ka = region(av, env, ta.shape)[1]
        rb, kb = region(bv, env, tb.shape)[1]
        rows, cols = ra[1] - ra[0], rb[1] - rb[0]
        if ka[1] - ka[0] != k or kb[1] - kb[0] != k:
            raise LowerError(f"gemm k={k}: the views give {ka}, {kb}")
        gm, gn = rows // SUB, cols // SUB
        tile = Tile(
            unit,
            self.layouts.mx7(av.name, ra[0], ka[0], gm),
            self.layouts.mx7(bv.name, rb[0], kb[0], gn),
            gm,
            gn,
            k // (32 * NK),
        )
        tile.reads = [region(av, env, ta.shape), region(bv, env, tb.shape)]
        return tile

    def finish(self, tile: Tile, out: tuple) -> None:
        tile.out = out
        self.tiles.append(tile)

    def slot(self, name: str, t: L.Type) -> tuple:
        n = len(self.slots.setdefault(name, []))
        self.slots[name].append(n)
        if self.drained_shape.setdefault(name, t.shape) != t.shape:
            raise LowerError(f"{name}: drained tiles of two shapes")
        rows, cols = t.shape
        return name, n * rows * cols * 2

    def vector(self, s: L.Par, env, core, vals) -> None:
        loads = [x for x in s.body if isinstance(x, L.Load)]
        if loads and all(x.src.name in vals and not x.src.axes for x in loads):
            task = self.epilogue(s, env, core, vals, loads)
        else:
            task = library.match(self, s, env, core)
            self.library.add(task.image)
        for t in self.tiles:
            if (
                any(overlaps(w, r) for w in t.writes for r in task.reads)
                and t not in task.tiles
            ):
                task.tiles.append(t)
        for v in self.vec:
            if any(overlaps(w, r) for w in v.writes for r in task.reads):
                task.after.append(v)
        self.vec.append(task)

    def epilogue(self, s, env, core, vals, loads) -> VecTask:
        ops = [x for x in s.body if isinstance(x, L.Op)]
        stores = [x for x in s.body if isinstance(x, L.Store)]
        if len(stores) != 1 or not isinstance(stores[0].value, L.Op):
            raise LowerError("an epilogue ends in one `store VIEW <- quantise VALUE`")
        st = stores[0]
        if st.value.op != "quantise":
            raise LowerError("an epilogue stores a quantised value")
        slots = [vals[ld.src.name] for ld in loads]
        t = self.types[st.dst.name]
        reg = region(st.dst, env, t.shape)
        (r0, _), (c0, _) = reg[1]
        out = self.layouts.mx7(st.dst.name, r0, c0, 64)
        task = VecTask(core, ([ld.name for ld in loads], ops, st.value.args[0].name))
        task.args = ["64", *slots, out]
        task.writes = [reg]
        task.tiles = [self.producer(x) for x in slots]
        task.lag = len(task.tiles) - 1
        task.work = 256 * 256 * (len(ops) + len(loads) + 1)
        return task

    def producer(self, slot) -> Tile:
        for t in reversed(self.tiles):
            if t.out == slot:
                return t
        raise LowerError(f"no tile drains {slot}")


__all__ = ["Tile", "Unroll", "VecTask", "overlaps", "region"]
