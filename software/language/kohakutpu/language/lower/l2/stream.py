"""L2 -> L1 for streams: 1-D elementwise tiles.

A tile of E elements is E/128 registers in groups of four; a pass is two tiles
(L1 buffers by tile parity), group i+1 unpacking while group i computes.
Input k is in v{8k}.. (a bank of four per group parity); results take sets of
four from v16.. (two per parity, and v8.. with one input), reusing an
operand's set where it dies.
"""

from kohakutpu.language.l2 import nodes as L
from kohakutpu.language.l2.verify import value
from kohakutpu.language.lower.l2.emit import (
    L1_WORDS,
    LOOP_S,
    MATH,
    Consts,
    LowerError,
    affine,
    uses,
)
from kohakutpu.language.text.writer import Text


class Stream:
    def __init__(self, name: str, par: L.Par, loop: L.For) -> None:
        self.name, self.par, self.loop = name, par, loop
        self.loads = [s for s in loop.body if isinstance(s, L.Load)]
        self.ops = [s for s in loop.body if isinstance(s, L.Op)]
        stores = [s for s in loop.body if isinstance(s, L.Store)]
        if len(stores) != 1 or not self.loads:
            raise LowerError("a stream has loads, ops and one store")
        self.store = stores[0]
        shapes = {ld.type.shape for ld in self.loads}
        if len(shapes) != 1 or len(next(iter(shapes))) != 1:
            raise LowerError("a stream's loads are one 1-D tile shape")
        (self.elems,) = next(iter(shapes))
        if self.elems % 512:
            raise LowerError(
                f"a stream tile is a multiple of 512 elements, not {self.elems}"
            )
        if len(self.loads) > 2:
            raise LowerError("a stream reads at most two inputs")

    def lower(self, out: Text, node: Text) -> None:
        nin = len(self.loads)
        words = self.elems // 16
        groups = self.elems // 512
        if 2 * words * (nin + 1) > L1_WORDS:
            raise LowerError(f"{nin + 1} streams of {words} words twice exceed L1")
        tiles = self.loop.hi - self.loop.lo
        if tiles % 2:
            raise LowerError("a stream runs its tiles in pairs")
        regs_in = {ld.name: 8 * k for k, ld in enumerate(self.loads)}
        inbuf = {ld.name: 2 * words * k for k, ld in enumerate(self.loads)}
        obuf = 2 * words * nin
        desc = {
            ld.name: f"a{0 if k == 0 else k + 1}" for k, ld in enumerate(self.loads)
        }
        consts = Consts()
        plans = [self.registers(regs_in, consts, q) for q in (0, 1)]
        img = f"{self.name}_stream"
        args = ["passes"] + [ld.name for ld in self.loads] + ["out"]
        out(f"image {img}({', '.join(args)})")
        with out.block():
            for ld in self.loads:
                out(f"desc {desc[ld.name]} = {ld.name} walk=(32, {words})")
            out(f"desc a1 = out walk=(32, {words})")
            for ld in self.loads:
                out(f"ainc {desc[ld.name]} += {self.step(ld.src)}")
            out(f"ainc a1 += {self.step(self.store.dst)}")
            consts.emit(out)
            for ld in self.loads:
                out(f"vfill {desc[ld.name]} -> l1[{inbuf[ld.name]}] rel")
            out(f"seti s{LOOP_S} = passes")
            out(f"loop s{LOOP_S}")
            with out.block():
                self.unpack(out, 0, groups, words, regs_in, inbuf, desc)
                for i in range(2 * groups):
                    if i < 2 * groups - 1:
                        self.unpack(out, i + 1, groups, words, regs_in, inbuf, desc)
                    lines, res = plans[i % 2]
                    for line in lines:
                        out(line)
                    p, g = i // groups, i % groups
                    if g == 0:
                        out("vsync pack on=drain slack=1")
                    for j in range(4):
                        out(
                            f"vpack v{res + j} -> l1[{obuf + words * p + (g * 4 + j) * 8}] f16"
                        )
                    if g == groups - 1:
                        out("vsync drain on=pack")
                        out(f"vdrain a1 <- l1[{obuf + words * p}] rel")
            out("halt")
        out("")
        for c in range(self.par.lo, self.par.hi):
            env = {self.par.var: c}
            addrs = [self.address(ld.src, env) for ld in self.loads]
            addrs.append(self.address(self.store.dst, env))
            node(f"send vc[{value(self.par.at, env)}]")
            with node.block():
                node(f"run {img}({tiles // 2}, {', '.join(addrs)})")

    def unpack(self, out, i, groups, words, regs_in, inbuf, desc) -> None:
        p, g = i // groups, i % groups
        if g == 0:
            out("vbar")
            out("vsync fill on=unpack")
            for ld in self.loads:
                out(
                    f"vfill {desc[ld.name]} -> l1[{inbuf[ld.name] + words * (1 - p)}] rel"
                )
        for j in range(4):
            for ld in self.loads:
                reg = regs_in[ld.name] + (i % 2) * 4 + j
                out(
                    f"vunpk v{reg} <- l1[{inbuf[ld.name] + words * p + (g * 4 + j) * 8}] f16"
                )

    def registers(self, regs_in: dict, consts: Consts, q: int) -> tuple:
        """Group parity q's op lines and the stored value's register set."""
        last = {n: ks[-1] for n, ks in uses(self.ops).items()}
        sv = self.store.value
        if not isinstance(sv, L.View):
            raise LowerError("a stream stores a value")
        last[sv.name] = len(self.ops)
        pool = [16 + 4 * q, 24 + 4 * q] + ([8 + 4 * q] if len(self.loads) == 1 else [])
        home = {n: r + 4 * q for n, r in regs_in.items()}
        lines = []
        for k, o in enumerate(self.ops):
            if o.op not in MATH:
                raise LowerError(f"no stream lowering for {o.op}")
            mnem, _ = MATH[o.op]
            srcs = []
            for a in o.args:
                if isinstance(a, L.Const):
                    srcs.append((None, consts.name(a.value)))
                elif a.axes:
                    raise LowerError("a stream op reads whole tiles")
                else:
                    srcs.append((a.name, home[a.name]))
            dying = [
                r
                for n, r in srcs
                if n is not None and n not in regs_in and last.get(n) == k
            ]
            dst = dying[0] if dying else None
            pool += [r for r in dict.fromkeys(dying) if r != dst]
            if dst is None:
                if not pool:
                    raise LowerError("a stream needs more register sets")
                dst = pool.pop(0)
            home[o.name] = dst
            for j in range(4):
                ops = [f"v{r + j}" if n is not None else r for n, r in srcs]
                lines.append(f"{mnem} v{dst + j}, {', '.join(ops)}")
        return lines, home[sv.name]

    def step(self, v: L.View) -> int:
        _, d = affine(v.axes[0].lo, {self.par.var: self.par.lo}, self.loop.var)
        return d * 2

    def address(self, v: L.View, env: dict) -> str:
        lo = value(v.axes[0].lo, {**env, self.loop.var: self.loop.lo})
        return f"{v.name} + {lo * 2}"


__all__ = ["Stream"]
