"""L2 -> L1 for row kernels: tiles of R x 128 values, one register a row,
with row reductions into statistics.

A group is 8 rows v{R}..v{R+7}, a statistics register (lane r = row r, a
chunk a statistic), temporaries T..T+3 and two partials successive
reductions alternate. Rows v0..v15, statistics v22 / v29, temporaries
v16..v21 / v23..v28, [128] values v30, v31.

- R = 16, lockstep: the two halves are two groups emitted side by side.
- R = 8, pipelined: stage A runs to the first row op reading a statistic,
  stage B the rest; step j runs A on tile parity j with B on parity 1 - j in
  one math run for the L1 scheduler; A uses v16..v21, B v23..v28.
"""

from kohakutpu.language.l2 import nodes as L
from kohakutpu.language.l2.verify import value
from kohakutpu.language.lower.l2.emit import (
    LOOP_S,
    MATH,
    Consts,
    LowerError,
    affine,
    uses,
)
from kohakutpu.language.text.writer import Text

#: A row reduction's combining instruction.
REDUCE = {"reduce.sum": "vadd", "reduce.max": "vmax", "reduce.min": "vmin"}
COLS = 128


class Group:
    def __init__(self, rows: int, stats: int, temps: int, parts: tuple) -> None:
        self.rows, self.stats, self.temps, self.parts = rows, stats, temps, parts


class Rows:
    def __init__(self, name, par, pre, loop) -> None:
        self.name, self.par, self.pre, self.loop = name, par, list(pre), loop
        self.loads = [s for s in loop.body if isinstance(s, L.Load)]
        self.ops = [s for s in loop.body if isinstance(s, L.Op)]
        stores = [s for s in loop.body if isinstance(s, L.Store)]
        if len(self.loads) != 1 or len(stores) != 1:
            raise LowerError("a row kernel has one tile load and one store")
        self.store = stores[0]
        shape = self.loads[0].type.shape
        if len(shape) != 2 or shape[1] != COLS or shape[0] not in (8, 16):
            raise LowerError(
                f"a row tile is [8, {COLS}] or [16, {COLS}], not {list(shape)}"
            )
        self.R = shape[0]
        if len(self.pre) > 2:
            raise LowerError("a row kernel holds at most two [128] values")
        self.types = {ld.name: ld.type.shape for ld in self.loads + self.pre}
        self.types.update({o.name: o.type.shape for o in self.ops})
        self.vecs = {ld.name: 30 + k for k, ld in enumerate(self.pre)}
        self.chunks: dict = {}
        self.squared: set = set()
        self.nred = 0

    def kind(self, name) -> str:
        t = self.types[name]
        if t == (self.R, COLS):
            return "row"
        if t == (self.R,):
            return "stat"
        if t == (COLS,):
            return "vec"
        raise LowerError(f"{name}: no row-kernel value of shape {list(t)}")

    # ------------------------------------------------------------ ops
    def split(self) -> int:
        """The first op of stage B: the first row op reading a statistic."""
        for k, o in enumerate(self.ops):
            if self.kind(o.name) == "row" and any(
                isinstance(a, L.View) and self.kind(a.name) == "stat" for a in o.args
            ):
                return k
        return len(self.ops)

    def compute(
        self, consts: Consts, lo: int, hi: int, groups: list, cur: str
    ) -> tuple:
        """Lines of ops[lo:hi] on `groups`, and the live row value after."""
        body = Text()
        use = uses(self.ops)
        sv = self.store.value
        later = {n: max(ks) for n, ks in use.items()}
        later[sv.name] = len(self.ops)
        fused = self.exp2d(use)
        for k in range(lo, hi):
            o = self.ops[k]
            if k in fused:
                continue
            if k - 1 in fused:
                # 2^(a - b) in one op: VEXP2D.
                o = L.Op(o.name, "exp2d", self.ops[k - 1].args, (), o.type)
            if o.op == "mul" and self.kind(o.name) == "row":
                rows = [
                    a
                    for a in o.args
                    if isinstance(a, L.View) and self.kind(a.name) == "row"
                ]
                readers = [self.ops[j] for j in use.get(o.name, [])]
                if (
                    len(rows) == 2
                    and rows[0].name == rows[1].name == cur
                    and readers
                    and all(r.op == "reduce.sum" for r in readers)
                ):
                    self.squared.add(o.name)
                    continue
            if o.op.startswith("reduce."):
                src = o.args[0].name
                self.chunks.setdefault(o.name, len(self.chunks))
                if src in self.squared:
                    self.reduce(body, "vadd", self.chunks[o.name], groups, square=True)
                elif src == cur:
                    self.reduce(body, REDUCE[o.op], self.chunks[o.name], groups)
                else:
                    raise LowerError(f"{o.name} reduces {src}, not the live row value")
                continue
            mnem, _ = MATH.get(o.op, ("vexp2d", 2) if o.op == "exp2d" else (None, 0))
            if mnem is None:
                raise LowerError(f"no row lowering for {o.op}")
            kinds = [
                self.kind(a.name) if isinstance(a, L.View) else "const" for a in o.args
            ]
            if self.kind(o.name) == "stat":
                if any(kd not in ("stat", "const") for kd in kinds):
                    raise LowerError(
                        f"{o.name}: a statistic is computed from statistics"
                    )
                self.chunks.setdefault(o.name, len(self.chunks))
                for g in groups:
                    ops = [
                        (
                            consts.name(a.value)
                            if kd == "const"
                            else f"v{g.stats}[{self.chunks[a.name]}:]"
                        )
                        for a, kd in zip(o.args, kinds, strict=True)
                    ]
                    body(
                        f"{mnem} v{g.stats}[{self.chunks[o.name]}:], {', '.join(ops)} vl=16"
                    )
                continue
            if self.kind(o.name) != "row":
                raise LowerError(f"{o.name}: a row kernel computes rows and statistics")
            for a, kd in zip(o.args, kinds, strict=True):
                if kd == "row" and a.name != cur:
                    raise LowerError(
                        f"{o.name} reads {a.name}; only {cur} is in registers"
                    )
            if later.get(cur, -1) > k:
                raise LowerError(f"{cur} is read after {o.name} rewrites it in place")
            if kinds[0] == "stat":
                raise LowerError(
                    f"{o.op}: a broadcast statistic is not the first operand"
                )
            for r in range(8):
                for g in groups:
                    ops = []
                    for a, kd in zip(o.args, kinds, strict=True):
                        if kd == "const":
                            ops.append(consts.name(a.value))
                        elif kd == "row":
                            ops.append(f"v{g.rows + r}")
                        elif kd == "vec":
                            ops.append(f"v{self.vecs[a.name]}")
                        else:
                            if not (len(a.axes) == 2 and a.axes[1].new):
                                raise LowerError(
                                    f"a statistic in a row op is {a.name}[:, *]"
                                )
                            ops.append(
                                f"bcast(v{g.stats}[{self.chunks[a.name]}], lane={r}, sh=3)"
                            )
                    body(f"{mnem} v{g.rows + r}, {', '.join(ops)}")
            cur = o.name
        if len(self.chunks) > 8:
            raise LowerError("more than eight row statistics")
        return body.lines, cur

    def exp2d(self, use: dict) -> set:
        """Indices of row `sub`s whose one reader is the next op, an `exp2`."""
        out = set()
        for k, o in enumerate(self.ops[:-1]):
            nxt = self.ops[k + 1]
            if (
                o.op == "sub"
                and self.kind(o.name) == "row"
                and nxt.op == "exp2"
                and nxt.args[0].name == o.name
                and use.get(o.name) == [k + 1]
            ):
                out.add(k)
        return out

    def reduce(self, out: Text, mnem: str, ch: int, groups: list, square=False) -> None:
        """Each group's rows (or their squares, summed) into chunk `ch` of its
        statistics: 64-lane halves into the temporaries, 32 lanes, 16 lanes
        into a partial register, then the merge tree in place."""
        part = self.nred % 2
        self.nred += 1
        for p in (0, 1):
            for k in range(4):
                for g in groups:
                    t = f"v{g.temps + k}[{4 * p}:]"
                    r = g.rows + 2 * k + p
                    if square:
                        out(f"vmul {t}, v{r}[4:], v{r}[4:] vl=64")
                        out(f"vfma {t}, v{r}[0:], v{r}[0:], {t} vl=64")
                    else:
                        out(f"{mnem} {t}, v{r}[0:], v{r}[4:] vl=64")
        for p in (0, 1):
            for k in range(4):
                for g in groups:
                    t = f"v{g.temps + k}"
                    out(f"{mnem} {t}[{4 * p}:], {t}[{4 * p}:], {t}[{4 * p + 2}:] vl=32")
        for r in range(8):
            for g in groups:
                t = f"v{g.temps + r // 2}"
                pr = f"v{g.parts[part]}"
                out(
                    f"{mnem} {pr}[{r}:], {t}[{4 * (r % 2)}:], {t}[{4 * (r % 2) + 1}:] vl=16"
                )
        for k, vl in ((8, ""), (4, " vl=64"), (2, " vl=32")):
            for g in groups:
                pr = f"v{g.parts[part]}"
                sel = f"[{k}:]" if k < 8 else ""
                out(f"{mnem} {pr}, {pr}, merge({pr}{sel}, {k}){vl}")
        for g in groups:
            pr = f"v{g.parts[part]}"
            out(f"{mnem} v{g.stats}[{ch}:], {pr}, merge({pr}[1:], 1) vl=16")

    # ---------------------------------------------------------- image
    def lower(self, out: Text, node: Text) -> None:
        tiles = self.loop.hi - self.loop.lo
        if tiles % 2:
            raise LowerError("a row kernel runs its tiles in pairs")
        consts = Consts()
        img = f"{self.name}_rows"
        params = ["passes", "x"] + [ld.name for ld in self.pre] + ["y"]
        words = self.R * COLS // 16
        out(f"image {img}({', '.join(params)})")
        with out.block():
            out(f"desc a0 = x walk=(32, {words})")
            out(f"desc a1 = y walk=(32, {words})")
            for k, ld in enumerate(self.pre):
                out(f"desc a{2 + k} = {ld.name} walk=(32, 8)")
            out(f"ainc a0 += {self.step(self.loads[0].src)}")
            out(f"ainc a1 += {self.step(self.store.dst)}")
            if self.R == 16:
                body = self.lockstep(consts)
            else:
                body = self.pipelined(consts)
            consts.emit(out)
            for k in range(len(self.pre)):
                out(f"vfill a{2 + k} -> l1[{384 + 8 * k}]")
            if self.pre:
                out("vbar")
                for k in range(len(self.pre)):
                    out(f"vunpk v{30 + k} <- l1[{384 + 8 * k}] f16")
            for line in body:
                out(line)
            out("halt")
        out("")
        for c in range(self.par.lo, self.par.hi):
            env = {self.par.var: c, self.loop.var: self.loop.lo}
            lo = value(self.loads[0].src.axes[0].lo, env)
            olo = value(self.store.dst.axes[0].lo, env)
            args = [str(tiles // 2), f"{self.loads[0].src.name} + {lo * 2 * COLS}"]
            args += [ld.src.name for ld in self.pre]
            args.append(f"{self.store.dst.name} + {olo * 2 * COLS}")
            node(f"send vc[{value(self.par.at, env)}]")
            with node.block():
                node(f"run {img}({', '.join(args)})")

    def lockstep(self, consts: Consts) -> list:
        groups = [
            Group(8 * h, 22 + 7 * h, 16 + 7 * h, (20 + 7 * h, 21 + 7 * h))
            for h in (0, 1)
        ]
        math, cur = self.compute(consts, 0, len(self.ops), groups, self.loads[0].name)
        self.check_stored(cur)
        body = Text()
        body("vfill a0 -> l1[0] rel")
        body("vmark m2 on=fill")
        body(f"seti s{LOOP_S} = passes")
        body(f"loop s{LOOP_S}")
        with body.block():
            for j in (0, 1):
                # The next tile into the other input buffer once it is read.
                body(f"vsync fill mark=m{1 - j}")
                body(f"vfill a0 -> l1[{128 - 128 * j}] rel")
                body(f"vmark m{3 - j} on=fill")
                body(f"vsync unpack mark=m{2 + j}")
                for r in range(16):
                    body(f"vunpk v{r} <- l1[{128 * j + r * 8}] f16")
                body(f"vmark m{j} on=unpack")
                for line in math:
                    body(line)
                body("vsync pack on=drain slack=1")
                for r in range(16):
                    body(f"vpack v{r} -> l1[{256 + 128 * j + r * 8}] f16")
                body("vsync drain on=pack")
                body(f"vdrain a1 <- l1[{256 + 128 * j}] rel")
        return body.lines

    def pipelined(self, consts: Consts) -> list:
        """Tile parity j: rows v{8j}.., x l1[64j], out l1[256 + 64j], m{j}
        marks x(j) filled. The last step's A runs on a fill past the end, and
        its tile is never written."""
        mid = self.split()
        a_math, b_math, cur_a = {}, {}, {}
        for j in (0, 1):
            ga = Group(8 * j, 22 + 7 * j, 16, (20, 21))
            gb = Group(8 * j, 22 + 7 * j, 23, (27, 28))
            self.nred = 0
            a_math[j], cur_a[j] = self.compute(consts, 0, mid, [ga], self.loads[0].name)
            b_math[j], cur = self.compute(consts, mid, len(self.ops), [gb], cur_a[j])
            self.check_stored(cur)
        body = Text()

        def unpack(j):
            body(f"vsync unpack mark=m{j}")
            for r in range(8):
                body(f"vunpk v{8 * j + r} <- l1[{64 * j + 8 * r}] f16")
            body("vsync fill on=unpack")
            body(f"vfill a0 -> l1[{64 * j}] rel")
            body(f"vmark m{j} on=fill")

        def step(j):
            unpack(j)
            a, b = a_math[j], b_math[1 - j]
            for k in range(max(len(a), len(b))):
                for side in (b, a):
                    if k < len(side):
                        body(side[k])
            body("vsync pack on=drain slack=1")
            for r in range(8):
                body(
                    f"vpack v{8 * (1 - j) + r} -> l1[{256 + 64 * (1 - j) + 8 * r}] f16"
                )
            body("vsync drain on=pack")
            body(f"vdrain a1 <- l1[{256 + 64 * (1 - j)}] rel")

        body("vfill a0 -> l1[0] rel")
        body("vmark m0 on=fill")
        body("vfill a0 -> l1[64] rel")
        body("vmark m1 on=fill")
        unpack(0)
        for line in a_math[0]:
            body(line)
        body(f"seti s{LOOP_S} = passes")
        body(f"loop s{LOOP_S}")
        with body.block():
            step(1)
            step(0)
        return body.lines

    def check_stored(self, cur: str) -> None:
        if self.store.value.name != cur:
            raise LowerError(
                f"the stored {self.store.value.name} is not the live row value"
            )

    def step(self, v: L.View) -> int:
        _, d = affine(v.axes[0].lo, {self.par.var: self.par.lo}, self.loop.var)
        return d * 2 * COLS


__all__ = ["REDUCE", "Rows"]
