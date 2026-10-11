"""Elementwise epilogue images: drained f16 tiles of 256 x 256 (tile=64x64
layout) through an op chain, quantised to MXFP7 A entries.

A sub-row is 4 rows x 256 columns, 64 words: GT4 unpacks give register b
rows 0..3 of K-block b. A unit is 1/h of a sub-row (R = 8/h registers a
value), h the least of 1, 2, 4 whose register sets fit. Units run two in
flight by bank b = unit % 2: value set s of bank b is v{(2s + b) R}..; after
the chain an F16 pack walked (2, 1) and a flat unpack put chunk 2i + q in the
quantiser's order, and MX7 packs make the entries. L1: input k's sub-rows by
parity at l1[128k + 64p], the round trip by bank, the entries by bank. Marks:
m{p} sub-row p filled, m{2+b} round trip packed, m{4+b} round trip read.
"""

from kohakutpu.ktpu.l2 import nodes as L
from kohakutpu.ktpu.l2.emit import LOOP_S, MATH, Consts, LowerError, Text, uses

SUBROW_WORDS = 64
COLS = 256


class Epilogue:
    def __init__(self, name: str, inputs: list, ops: list, result: str) -> None:
        """`inputs`: value names in argument order; `ops`: the chain; `result`
        the value quantised."""
        self.name, self.inputs, self.ops, self.result = name, inputs, ops, result
        for o in ops:
            if o.op not in MATH:
                raise LowerError(f"no epilogue lowering for {o.op}")
        for h in (1, 2, 4):
            self.h, self.R = h, 8 // h
            try:
                self.consts = Consts()
                self.chain = [self.registers(b) for b in (0, 1)]
                break
            except LowerError:
                continue
        else:
            raise LowerError(f"{name}: the chain's values do not fit the registers")

    def sets(self) -> int:
        return 16 // self.R

    def reg(self, s: int, b: int) -> int:
        return (2 * s + b) * self.R

    def registers(self, b: int) -> tuple:
        """Bank b's chain lines and the result's first register."""
        nin = len(self.inputs)
        if nin > self.sets():
            raise LowerError("inputs exceed the sets")
        home = {n: self.reg(k, b) for k, n in enumerate(self.inputs)}
        pool = [self.reg(s, b) for s in range(nin, self.sets())]
        last = {n: ks[-1] for n, ks in uses(self.ops).items()}
        last[self.result] = len(self.ops)
        lines = []
        for k, o in enumerate(self.ops):
            mnem, _ = MATH[o.op]
            srcs = []
            for a in o.args:
                if isinstance(a, L.Const):
                    srcs.append((None, self.consts.name(a.value)))
                elif a.axes:
                    raise LowerError("an epilogue op reads whole tiles")
                else:
                    srcs.append((a.name, home[a.name]))
            dying = [r for n, r in srcs if n is not None and last.get(n) == k]
            dst = dying[0] if dying else None
            pool += [r for r in dict.fromkeys(dying) if r != dst]
            if dst is None:
                if not pool:
                    raise LowerError("an epilogue needs more register sets")
                dst = pool.pop(0)
            home[o.name] = dst
            for j in range(self.R):
                ops = [f"v{r + j}" if n is not None else r for n, r in srcs]
                lines.append(f"{mnem} v{dst + j}, {', '.join(ops)}")
        return lines, home[self.result]

    # ------------------------------------------------------------ image
    def text(self, out: Text) -> None:
        h, R, nin = self.h, self.R, len(self.inputs)
        rt = 128 * nin
        ent = rt + 16 * R
        args = ["subrows"] + [f"in{k}" for k in range(nin)] + ["out"]
        out(f"image {self.name}({', '.join(args)})")
        with out.block():
            for k in range(nin):
                out(f"desc a{2 * k} = in{k} walk=(32, {SUBROW_WORDS})")
            for q in range(h):
                base = "out" if q == 0 else f"out + {q * 64 // h}K"
                out(f"desc a{1 + 2 * q} = {base} walk=((32, 8), (16K, {4 // h}))")
            for k in range(nin):
                out(f"ainc a{2 * k} += 2K")
            for q in range(h):
                out(f"ainc a{1 + 2 * q} += 256")
            self.consts.emit(out)
            for p in (0, 1):
                for k in range(nin):
                    out(f"vfill a{2 * k} -> l1[{128 * k + 64 * p}] rel")
                out(f"vmark m{p} on=fill")
            self.head(out, 0, rt)
            out(f"seti s{LOOP_S} = subrows/2 - 1")
            out(f"loop s{LOOP_S}")
            with out.block():
                for k in range(1, 2 * h + 1):
                    self.head(out, k % (2 * h), rt)
                    self.tail(out, (k - 1) % (2 * h), rt, ent)
            for k in range(1, 2 * h):
                self.head(out, k, rt)
                self.tail(out, k - 1, rt, ent)
            self.tail(out, 2 * h - 1, rt, ent)
            out("halt")
        out("")

    def head(self, out: Text, n: int, rt: int) -> None:
        h, R, nin = self.h, self.R, len(self.inputs)
        p, q, b = (n // h) % 2, n % h, n % 2
        if q == 0:
            out(f"vsync unpack mark=m{p}")
        for j in range(R):
            for k in range(nin):
                at = 128 * k + 64 * p + (SUBROW_WORDS // h) * q + 8 * j
                out(f"vunpk v{self.reg(k, b) + j} <- l1[{at}] gt4")
        if q == h - 1:
            out("vsync fill on=unpack")
            for k in range(nin):
                out(f"vfill a{2 * k} -> l1[{128 * k + 64 * p}] rel")
            out(f"vmark m{p} on=fill")
        lines, res = self.chain[b]
        for line in lines:
            out(line)
        out(f"vsync pack mark=m{4 + b}")
        for j in range(R):
            out(f"vpack v{res + j} -> l1[{rt + 8 * R * b + 8 * j}] f16 walk=(2, 1)")
        out(f"vmark m{2 + b} on=pack")

    def tail(self, out: Text, n: int, rt: int, ent: int) -> None:
        R = self.R
        b, q = n % 2, n % self.h
        out(f"vsync unpack mark=m{2 + b}")
        for j in range(R):
            out(f"vunpk v{self.reg(0, b) + j} <- l1[{rt + 8 * R * b + 8 * j}] f16")
        out(f"vmark m{4 + b} on=unpack")
        out("vsync pack on=drain slack=1")
        for j in range(R):
            out(f"vpack v{self.reg(0, b) + j} -> l1[{ent + 4 * R * b + 4 * j}] mx7a")
        out("vsync drain on=pack")
        out(f"vdrain a{1 + 2 * q} <- l1[{ent + 4 * R * b}] rel")


__all__ = ["COLS", "Epilogue"]
