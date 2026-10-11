"""An `fn NAME.l1` body to an L1 `Program`: the node's statements, with each
unit's words.

    send vc[c]                      # block: a vector core's words
        desc a0 = x + c*64K walk=(32, 128)      # base, dims (stride, bound)
        run silu_stream(8)          # the image's words, then RUN
    send mg[i]                      # block: a cluster's words
        fill A0 <- ADDR n=128 [at=ENTRY]
        gemm 64x64x2 a=A0 b=B0 [acc] [aoff=] [boff=] [emit=ADDR]
        drain ADDR n=4096 [fused] / drain n=128 to=vc[0] l1=WORD [ack=(x, y)]
    move / t = move posted          # block: copy|quant|gt4 SRC -> DST N=
    t = mark vc[0] / wait vc[0] [upto=t] / wait moves t / barrier
    fetch vc[0] from ADDR n=N

Parameters are buffers: the caller binds each to its address, and the body is
read with those addresses as constants. A `desc` here, or leading an image,
is `image.desc`.
"""

from kohakutpu.compiler.emit import image as vector
from kohakutpu.compiler.encode import cluster as C
from kohakutpu.compiler.encode import mover as M
from kohakutpu.compiler.encode import vector as A
from kohakutpu.compiler.program import Program
from kohakutpu.language.text.meta import Expander
from kohakutpu.language.text.syntax import Arrow, Call, Dims, Int, Name, Tuple, View

UNITS = {"vc": "VC", "mg": "MG"}


class Flits:
    """Words for one unit, as `Program.send` takes them."""

    def __init__(self, words: list) -> None:
        self.words = words

    def flits(self) -> list:
        return self.words


class NodeReader:
    def __init__(self, module, program: Program, entry=vector.UNKNOWN) -> None:
        self.module = module
        self.r = module.reader()
        self.fail = self.r.fail
        self.p = program
        self.entry = entry
        self.tokens: dict = {}
        #: (image, args) -> its words, encoded once.
        self.images: dict = {}
        #: cluster -> [(addr, sub-tiles, stmt)] emitted and not yet collected.
        self.emits: dict = {}

    def stmts(self, stmts) -> None:
        for st in stmts:
            self.stmt(st)

    def stmt(self, st) -> None:
        if st.target is not None and st.op not in ("mark", "move"):
            self.fail(st, f"`{st.op}` binds no name")
        if st.body and st.op not in ("send", "move"):
            self.fail(st, f"`{st.op}` takes no block")
        match st.op:
            case "send":
                self.send(st)
            case "move":
                self.move(st)
            case "mark":
                if st.target is None:
                    self.fail(st, "t = mark UNIT")
                self.tokens[st.target] = self.p.mark(self.unit(st, self.one(st)))
            case "wait":
                self.wait(st)
            case "barrier":
                if st.args:
                    self.fail(st, "barrier takes nothing")
                self.p.barrier()
            case "fetch":
                self.fetch(st)
            case _:
                self.fail(st, f"no L1 node statement {st.op!r}")

    def send(self, st) -> None:
        coord = self.unit(st, self.one(st))
        if coord in self.p.units("MG"):
            ops = [self.cluster(s) for s in st.body]
            for s, op in zip(st.body, ops, strict=True):
                self.collect(coord, s, op)
            self.p.send(coord, *ops)
            return
        words: list = []
        descs: dict = {}
        for s in st.body:
            if s.op == "desc":
                descs.update(self.desc(s))
            elif s.op == "run":
                words += self.run(s, descs)
                descs = {}
            else:
                self.fail(s, f"a vector core's send holds desc and run, not {s.op!r}")
        if descs:
            words += [A.desc_flit(ad, f, v) for (ad, f), v in sorted(descs.items())]
        self.p.send(coord, Flits(words))

    def desc(self, st) -> dict:
        return vector.desc(self.fail, st)

    def run(self, st, descs: dict) -> list:
        call = self.one(st)
        if not isinstance(call, Call):
            self.fail(st, "run IMAGE(args)")
        args = tuple(self.const(st, a) for a in call.args)
        key = (call.name, args)
        if key not in self.images:
            self.images[key] = vector.image(self.module, call.name, args, self.entry)
        words, own = self.images[key]
        descs = {**descs, **own}
        out = [A.desc_flit(ad, f, v) for (ad, f), v in sorted(descs.items())]
        out += [A.imem_flit(i, w) for i, w in enumerate(words)]
        out.append(A.run_flit(0))
        return out

    # ---------------------------------------------------------- clusters
    def cluster(self, st):
        pos, kw = st.positional(), st.kwargs()
        flags = {p.text for p in pos if isinstance(p, Name)}
        if st.body or st.target is not None:
            self.fail(st, f"`{st.op}` takes no block and binds no name")
        match st.op:
            case "fill":
                if len(pos) != 3 or pos[1] != Arrow("<-") or "n" not in kw:
                    self.fail(st, "fill A0|A1|B0|B1 <- ADDR n=ENTRIES [at=ENTRY]")
                sel, bank = self.side(st, pos[0])
                return C.Fill(
                    self.const(st, pos[2]),
                    self.const(st, kw["n"]),
                    sel=sel,
                    eoff=self.const(st, kw.get("at", Int(0))),
                    fbank=bank,
                )
            case "gemm":
                if not pos or not isinstance(pos[0], Dims) or len(pos[0].values) != 3:
                    self.fail(
                        st, "gemm GMxGNxNK a=A0 b=B0 [acc] [aoff=] [boff=] [emit=ADDR]"
                    )
                if flags - {"acc"}:
                    self.fail(st, f"gemm flags: acc; got {sorted(flags)}")
                acc = "acc" in flags or bool(self.const(st, kw.get("acc", Int(0))))
                gm, gn, nk = pos[0].values
                sa, abank = self.side(st, kw.get("a", Name("A0")))
                sb, bbank = self.side(st, kw.get("b", Name("B0")))
                if (sa, sb) != (0, 1):
                    self.fail(st, "a= names an A bank, b= a B bank")
                emit = "emit" in kw
                g = C.Gemm(
                    gm,
                    gn,
                    nk,
                    acc=acc,
                    aoff=self.const(st, kw.get("aoff", Int(0))),
                    boff=self.const(st, kw.get("boff", Int(0))),
                    abank=abank,
                    bbank=bbank,
                    emit=emit,
                    addr=self.const(st, kw["emit"]) if emit else 0,
                )
                try:
                    g.flits()
                except ValueError as e:
                    self.fail(st, str(e))
                return g
            case "drain":
                if "n" not in kw or flags - {"fused"}:
                    self.fail(st, "drain ADDR n=N [fused] | drain n=N to=vc[i] l1=WORD")
                n = self.const(st, kw["n"])
                if "to" not in kw:
                    addrs = [p for p in pos if not isinstance(p, Name)]
                    if len(addrs) != 1:
                        self.fail(st, "drain ADDR n=N [fused]")
                    return C.Drain(self.const(st, addrs[0]), n, fuse="fused" in flags)
                dst = self.unit(st, kw["to"])
                ack = self.coord(st, kw["ack"]) if "ack" in kw else (0, 0)
                return C.Drain(
                    32 * self.const(st, kw.get("l1", Int(0))),
                    n,
                    fuse="fused" in flags,
                    dst=dst,
                    dflags=self.const(st, kw.get("flags", Int(0))),
                    ack=ack,
                )
        self.fail(st, f"a cluster's send holds fill, gemm, drain; not {st.op!r}")

    def collect(self, coord, st, op) -> None:
        """A fused DRAIN takes the sub-tiles an emitting GEMM hands out, in
        order: one without an emit before it waits forever."""
        pending = self.emits.setdefault(coord, [])
        if isinstance(op, C.Gemm) and op.emit:
            pending.append((op.addr, op.gm * op.gn, st))
        elif isinstance(op, C.Drain) and op.fuse:
            if not pending:
                self.fail(
                    st,
                    "a fused drain collects an emitting gemm's sub-tiles; none is open",
                )
            addr, n, _ = pending.pop(0)
            if (op.addr, op.n) != (addr, n):
                self.fail(st, f"the open emit streams {n} sub-tiles to {addr:#x}")

    def finish(self) -> None:
        for pending in self.emits.values():
            if pending:
                self.fail(pending[0][2], "an emitting gemm no fused drain collects")

    def side(self, st, t) -> tuple:
        if (
            isinstance(t, Name)
            and len(t.text) == 2
            and t.text[0] in "AB"
            and t.text[1] in "01"
        ):
            return "AB".index(t.text[0]), int(t.text[1])
        self.fail(st, f"wanted an L1 side and bank A0 A1 B0 B1, got {t!r}")

    def coord(self, st, t) -> tuple:
        if isinstance(t, Tuple) and len(t.items) == 2:
            return tuple(self.const(st, x) for x in t.items)
        return self.unit(st, t)

    # ------------------------------------------------------------- mover
    def move(self, st) -> None:
        pos = st.positional()
        posted = pos == [Name("posted")]
        if (pos and not posted) or not st.body:
            self.fail(st, "move [posted] (a block of copy / quant / gt4)")
        if posted != (st.target is not None):
            self.fail(st, "t = move posted (a posted move's token), or move")
        ops = [self.mover_op(s) for s in st.body]
        if posted:
            self.tokens[st.target] = ("moves", self.p.post(*ops))
        else:
            self.p.move(*ops)

    def mover_op(self, st):
        pos, kw = st.positional(), st.kwargs()
        size = {"copy": "bytes", "quant": "entries", "gt4": "groups"}.get(st.op)
        if size is None:
            self.fail(st, f"a move holds copy, quant, gt4; not {st.op!r}")
        if len(pos) != 3 or pos[1] != Arrow("->") or set(kw) != {size}:
            self.fail(st, f"{st.op} SRC -> DST {size}=N")
        src, dst, n = (
            self.const(st, pos[0]),
            self.const(st, pos[2]),
            self.const(st, kw[size]),
        )
        return {"copy": M.Copy, "quant": M.Quantise, "gt4": M.Transpose4}[st.op](
            src, dst, n
        )

    def wait(self, st) -> None:
        pos = st.positional()
        if pos and pos[0] == Name("moves"):
            got = (
                self.tokens.get(pos[1].text)
                if len(pos) == 2 and isinstance(pos[1], Name)
                else None
            )
            if not isinstance(got, tuple):
                self.fail(st, "wait moves T, T from `T = move posted`")
            self.p.wait_moves(got[1])
            return
        coord = self.unit(st, self.one(st))
        kw = st.kwargs()
        token = None
        if "upto" in kw:
            t = kw["upto"]
            if not isinstance(t, Name) or not isinstance(self.tokens.get(t.text), int):
                self.fail(st, f"upto= names a mark, got {t!r}")
            token = self.tokens[t.text]
        self.p.wait(coord, token)

    def fetch(self, st) -> None:
        pos = st.positional()
        if len(pos) != 3 or pos[1] != Name("from") or "n" not in st.kwargs():
            self.fail(st, "fetch UNIT from ADDR n=N")
        coord = self.unit(st, pos[0])
        self.p.fetch(coord, self.const(st, pos[2]), self.const(st, st.kwargs()["n"]))

    # ------------------------------------------------------------ terms
    def one(self, st):
        pos = st.positional()
        if len(pos) != 1:
            self.fail(st, f"`{st.op}` takes one operand")
        return pos[0]

    def unit(self, st, t) -> tuple:
        if isinstance(t, View) and t.name in UNITS and len(t.slices) == 1:
            i = self.const(st, t.slices[0])
            have = self.p.units(UNITS[t.name])
            if not 0 <= i < len(have):
                self.fail(st, f"{t.name}[{i}]: this machine has {len(have)}")
            return have[i]
        self.fail(st, f"wanted a unit vc[i] / mg[i], got {t!r}")

    def const(self, st, t) -> int:
        if isinstance(t, Int):
            return t.value
        self.fail(st, f"wanted a constant (parameters are bound), got {t!r}")


def program(module, name: str, binds: dict, machine, entry=vector.UNKNOWN) -> Program:
    """`fn name.l1` with each parameter bound to an address."""
    body = module.body(name, "l1")
    r = module.reader()
    missing = [p for p, _ in body.params if p not in binds]
    if missing:
        raise ValueError(f"{name}.l1: unbound parameters {missing}")
    macros = {m.name: (m.params, m.stmts) for m in module.macros.values()}
    stmts = Expander(macros, r.fail, unroll_all=True).stmts(body.stmts, dict(binds))
    p = Program(machine)
    reader = NodeReader(module, p, entry)
    reader.stmts(stmts)
    reader.finish()
    return p


__all__ = ["NodeReader", "program"]
