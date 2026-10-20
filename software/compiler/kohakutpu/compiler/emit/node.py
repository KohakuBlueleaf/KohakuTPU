"""An `fn NAME.l1` body to L1 `Program`s: the node's statements, with each
unit's words; docs/projects/kohakutpu/ir/ktpu.md s2 is the statement table.

Parameters are buffers: the caller binds each to its address, and the body is
read with those addresses as constants. A field written `rN` or `ADDR + rN`
takes its value from register N when the node reaches the word (a PATCH); a
unit in another mesh is `meshM.mg[i]`, a drain's destination only; `node N`
blocks are one program per node.
"""

from dataclasses import replace

from kohakuaccel.compiler.package import mover as PM
from kohakuaccel.compiler.package.format import Alu, Cond
from kohakuaccel.compiler.program import TMP, Patch
from kohakutpu.compiler.emit import image as vector
from kohakutpu.compiler.emit.sru import RowReader
from kohakutpu.compiler.encode import cluster as C
from kohakutpu.compiler.encode import mover as M
from kohakutpu.compiler.encode import vector as A
from kohakutpu.compiler.isa.vector import ISA as VEC
from kohakutpu.compiler.program import Program
from kohakutpu.language.text.meta import Expander
from kohakutpu.language.text.syntax import (
    Arrow,
    Assign,
    Call,
    Compare,
    Dims,
    Int,
    Kw,
    Name,
    Offset,
    Tuple,
    View,
)

UNITS = {"vc": "VC", "mg": "MG", "sr": "SR"}
#: The node processor's coordinate, a mesh corner (sn_hub.v PE_X/PE_Y).
NODE = (0, 0)
ALU = {
    "set": Alu.MOV,
    "add": Alu.ADD,
    "sub": Alu.SUB,
    "mul": Alu.MUL,
    "and": Alu.AND,
    "or": Alu.OR,
    "xor": Alu.XOR,
    "shl": Alu.SHL,
    "shr": Alu.SHR,
    "min": Alu.MIN,
    "max": Alu.MAX,
    "divu": Alu.DIVU,
    "remu": Alu.REMU,
}
#: Control-region offsets by name (control-registers.md s7.2).
CTRL = {
    "dbell": 0x10,
    "mvstat": 0x20,
    "dbcnt": 0x28,
    "irq": 0x38,
    "il_ctl": 0xC0,
    "il_mesh": 0xC8,
    "il_ring": 0xD0,
    "il_consume": 0xE0,
    "sig": 0xE0,
    "il_select": 0xE8,
    "dcache": 0x1C8,
    "xf_sel": 0x1D0,
    "xf_data": 0x1D8,
}
EW = {8: PM.W8, 16: PM.W16, 32: PM.W32}
#: Flit types a node sends raw (flit-format.md s2).
CU_CTRL, CU_DATA = 0x7, 0x8
#: CU_DATA descriptor fields (flit-format.md s4.7), as bit positions.
CD_BUF, CD_OFF, CD_LEN, CD_FLAGS, CD_ACK_Y, CD_ACK_X = 248, 232, 224, 216, 212, 208
#: A CU_CTRL request's index (control-registers.md s1.1).
CTRL_INDEX = 240


class Flits:
    """Words for one unit, as `Program.send` takes them, and their patches."""

    def __init__(self, words: list, patches=()) -> None:
        self.words = words
        self.patches = tuple(patches)

    def flits(self) -> list:
        return self.words


class Units:
    """Unit names in vector-core images and descriptors, resolved on the
    program's machine: ``units(st, t) -> (mesh, (x, y))``; `home` is the
    program's mesh."""

    def __init__(self, reader) -> None:
        self.reader = reader
        self.home = reader.p.machine.default

    def __call__(self, st, t) -> tuple:
        return self.reader.target(st, t)


class NodeReader:
    def __init__(self, module, program: Program, entry=vector.UNKNOWN) -> None:
        self.module = module
        self.r = module.reader()
        self.fail = self.r.fail
        self.p = program
        self.entry = entry
        self.tokens: dict = {}
        self.units = Units(self)
        self.images = vector.Images(module, entry, self.units)
        #: cluster -> [(addr, sub-tiles, stmt)] emitted and not yet collected.
        self.emits: dict = {}
        #: core -> the encoded image its IMEM holds, along straight-line code
        self.resident: dict = {}
        #: row unit -> the configuration words this program wrote it
        self.configs: dict = {}

    def stmts(self, stmts) -> None:
        for st in stmts:
            self.stmt(st)

    def stmt(self, st) -> None:
        if st.target is not None and st.op not in (
            "mark",
            "move",
            *ALU,
            "load",
            "ctrl",
        ):
            self.fail(st, f"`{st.op}` binds no name")
        if st.body and st.op not in ("send", "move"):
            self.fail(st, f"`{st.op}` takes no block")
        if st.target is not None and st.op in (*ALU, "load", "ctrl"):
            self.register_op(st)
            return
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
            case "ring":
                self.ring(st)
            case "store" | "ctrl":
                self.write(st)
            case "signal":
                pos = [self.const(st, t) for t in st.positional()]
                if not 1 <= len(pos) <= 2:
                    self.fail(st, "signal V32 [V64]")
                self.p.signal(*pos)
            case "settle":
                self.p.settle(self.const(st, self.one(st)))
            case "label":
                self.resident.clear()  # reached from more than one place
                self.configs.clear()
                self.p.label(self.name(st, self.one(st)))
            case "goto":
                self.resident.clear()
                self.configs.clear()
                self.goto(st)
            case "push":
                self.push(st)
            case "recv":
                self.recv(st)
            case "query":
                self.query(st)
            case _:
                self.fail(st, f"no L1 node statement {st.op!r}")

    # ------------------------------------------------------------- values
    def reg(self, st, t, limit: int = TMP) -> int:
        """`rN` -> N; r15 is the patch scratch and not the program's."""
        if isinstance(t, Name) and t.text[:1] == "r" and t.text[1:].isdigit():
            n = int(t.text[1:])
            if n < limit:
                return n
        self.fail(st, f"wanted a register r0..r{limit - 1}, got {t!r}")

    def is_reg(self, t) -> bool:
        return isinstance(t, Name) and t.text[:1] == "r" and t.text[1:].isdigit()

    def val(self, st, t) -> tuple:
        """``(constant, register or None)`` of a field written `C`, `rN` or
        `C + rN`."""
        if isinstance(t, Int):
            return t.value, None
        if self.is_reg(t):
            return 0, self.reg(st, t)
        if isinstance(t, Offset) and self.is_reg(t.base) and isinstance(t.off, Int):
            return t.off.value, self.reg(st, t.base)
        self.fail(
            st,
            f"wanted a constant, rN or C + rN (compute others with ALU ops), got {t!r}",
        )

    def name(self, st, t) -> str:
        if not isinstance(t, Name):
            self.fail(st, f"wanted a name, got {t!r}")
        return t.text

    # ---------------------------------------------------------- registers
    def register_op(self, st) -> None:
        rd = self.reg(st, Name(st.target))
        pos = st.positional()
        if st.op == "load":
            if len(pos) != 1:
                self.fail(st, "rN = load ADDR [+ rM]")
            c, index = self.val(st, pos[0])
            self.p.load(rd, c, index)
            return
        if st.op == "ctrl":
            self.p.load(rd, self.offset(st, self.one(st)), ctrl=True)
            return
        op = ALU[st.op]
        if op == Alu.MOV:
            if len(pos) != 1:
                self.fail(st, "rN = set VALUE | rM")
            a, b = 0, pos[0]
        else:
            if len(pos) != 2:
                self.fail(st, f"rN = {st.op} rA, B")
            a, b = self.reg(st, pos[0]), pos[1]
        if self.is_reg(b):
            self.p.alu(rd, op, a, self.reg(st, b), imm=False)
        elif isinstance(b, Int):
            self.p.alu(rd, op, a, b.value & ((1 << 64) - 1))
        else:
            self.fail(st, f"an ALU operand is a constant or a register, got {b!r}")

    def goto(self, st) -> None:
        pos = st.positional()
        signed = Name("signed") in pos
        pos = [p for p in pos if p != Name("signed")]
        target = self.name(st, pos[0]) if pos else None
        if len(pos) == 1:
            self.p.jump(target)
            return
        if len(pos) != 3 or pos[1] != Name("if") or not isinstance(pos[2], Compare):
            self.fail(st, "goto LABEL [if rA CMP B] [signed]")
        cmp = pos[2]
        a, b = self.reg(st, cmp.a), cmp.b
        lt, ge = (Cond.LT, Cond.GE) if signed else (Cond.LTU, Cond.GEU)
        if self.is_reg(b):
            rb = self.reg(st, b)
            cond, ra, rb = {
                "==": (Cond.EQ, a, rb),
                "!=": (Cond.NE, a, rb),
                "<": (lt, a, rb),
                ">=": (ge, a, rb),
                ">": (lt, rb, a),
                "<=": (ge, rb, a),
            }[cmp.op]
            self.p.jump(target, cond, ra, rb, imm=False)
            return
        k = self.const(st, b)
        cond, k = {
            "==": (Cond.EQ, k),
            "!=": (Cond.NE, k),
            "<": (lt, k),
            ">=": (ge, k),
            ">": (ge, k + 1),
            "<=": (lt, k + 1),
        }[cmp.op]
        self.p.jump(target, cond, a, k & ((1 << 64) - 1))

    # ------------------------------------------------ interlink and memory
    def ring(self, st) -> None:
        kw = st.kwargs()
        flags = {p.text for p in st.positional() if isinstance(p, Name)}
        if (
            "mesh" not in kw
            or flags - {"plain"}
            or set(kw) - {"mesh", "slot", "amount"}
        ):
            self.fail(st, "ring mesh=M [slot=S] [amount=N] [plain]")
        self.p.ring(
            self.const(st, kw["mesh"]),
            self.const(st, kw.get("slot", Int(0))),
            self.const(st, kw.get("amount", Int(1))),
            fence="plain" not in flags,
        )

    def offset(self, st, t) -> int:
        if isinstance(t, Name) and t.text in CTRL:
            return CTRL[t.text]
        return self.const(st, t)

    def write(self, st) -> None:
        pos = st.positional()
        if len(pos) != 3 or pos[1] != Arrow("<-"):
            self.fail(
                st, f"{st.op} {'OFFSET' if st.op == 'ctrl' else 'ADDR'} <- VALUE | rN"
            )
        at = self.offset(st, pos[0]) if st.op == "ctrl" else self.const(st, pos[0])
        regs = self.is_reg(pos[2])
        v = self.reg(st, pos[2]) if regs else self.const(st, pos[2]) & ((1 << 64) - 1)
        (self.p.ctrl if st.op == "ctrl" else self.p.store)([(at, v)], regs)

    def push(self, st) -> None:
        pos, kw = st.positional(), st.kwargs()
        flags = {p.text for p in pos if isinstance(p, Name)}
        pos = [p for p in pos if not isinstance(p, Name)]
        if len(pos) != 3 or pos[1] != Arrow("<-") or "n" not in kw:
            self.fail(
                st, "push UNIT buf=B at=OFF <- ADDR n=N [signal] [ack=UNIT] [wait]"
            )
        coord = self.unit(st, pos[0])
        n = self.const(st, kw["n"])
        if not 1 <= n <= 256:
            self.fail(st, f"a CU_DATA burst is 1..256 words, got {n}")
        ack = self.coord(st, kw["ack"]) if "ack" in kw else (0, 0)
        desc = (
            self.const(st, kw.get("buf", Int(0))) << CD_BUF
            | self.const(st, kw.get("at", Int(0))) << CD_OFF
            | (n - 1) << CD_LEN
            | int("signal" in flags or "wait" in flags) << CD_FLAGS
            | ack[1] << CD_ACK_Y
            | ack[0] << CD_ACK_X
        )
        self.p.push(coord, desc, self.const(st, pos[2]), n, ack="wait" in flags)

    def recv(self, st) -> None:
        pos, kw = st.positional(), st.kwargs()
        if len(pos) != 2 or pos[0] != Arrow("->") or "n" not in kw:
            self.fail(st, "recv n=N -> ADDR [from=UNIT]")
        src = self.unit(st, kw["from"]) if "from" in kw else None
        self.p.recv(self.const(st, pos[1]), self.const(st, kw["n"]), src)

    def query(self, st) -> None:
        pos, kw = st.positional(), st.kwargs()
        if len(pos) != 3 or pos[1] != Arrow("->") or "index" not in kw:
            self.fail(st, "query UNIT index=I -> ADDR")
        coord = self.unit(st, pos[0])
        self.p.send_raw(coord, CU_CTRL, [self.const(st, kw["index"]) << CTRL_INDEX])
        self.p.recv(self.const(st, pos[2]), 1, coord)

    # -------------------------------------------------------------- sends
    def send(self, st) -> None:
        coord = self.unit(st, self.one(st))
        if coord in self.p.units("MG"):
            ops = [self.cluster(s) for s in st.body]
            for s, op in zip(st.body, ops, strict=True):
                self.collect(coord, s, op)
            self.p.send(coord, *ops)
            return
        if coord in self.p.units("SR"):
            held = self.configs.setdefault(coord, {})
            self.p.send(coord, *RowReader(self, held).ops_of(st.body))
            return
        words: list = []
        fixes: list = []
        descs: dict = {}
        #: (descriptor, 0) -> the Patch of a base written with a register
        held: dict = {}

        def emit(d: dict) -> None:
            for key, v in sorted(d.items()):
                p = held.pop(key, None)
                if p is not None:
                    fixes.append(Patch(len(words), p.segments, p.reg, p.const))
                words.append(A.desc_flit(key[0], key[1], v))

        for s in st.body:
            if s.op == "desc":
                got, fix = self.desc(s)
                descs.update(got)
                held.update(fix)
            elif s.op == "run":
                call = self.one(s)
                if not isinstance(call, Call):
                    self.fail(s, "run IMAGE(args)")
                img, own = self.images(
                    call.name, tuple(self.const(s, a) for a in call.args)
                )
                for key in own:
                    held.pop(key, None)
                emit({**descs, **own})
                if self.resident.get(coord) is not img:
                    words += [A.imem_flit(i, w) for i, w in enumerate(img)]
                    self.resident[coord] = img
                words.append(A.run_flit(0))
                descs = {}
            else:
                self.fail(s, f"a vector core's send holds desc and run, not {s.op!r}")
        emit(descs)
        self.p.send(coord, Flits(words, fixes))

    def desc(self, st) -> tuple:
        """``({(ad, field): value}, {(ad, 0): Patch})``: a base written `rN` or
        `C + rN` is patched into DESC's value and value_hi fields."""
        heads = [
            a
            for a in st.args
            if isinstance(a, Assign)
            and isinstance(a.lhs, Name)
            and a.lhs.text != "walk"
        ]
        fix = {}
        if len(heads) == 1 and (
            self.is_reg(heads[0].rhs) or isinstance(heads[0].rhs, Offset)
        ):
            c, reg = self.val(st, heads[0].rhs)
            st = replace(
                st,
                args=[Assign(a.lhs, Int(c)) if a is heads[0] else a for a in st.args],
            )
            lo, wlo = VEC.DESC.span("value")
            hi, whi = VEC.DESC.span("value_hi")
            ad = int(heads[0].lhs.text[1:])
            fix[(ad, 0)] = Patch(0, ((lo, wlo, 0), (hi, whi, wlo)), reg, c)
        return vector.desc(self.fail, st, self.units), fix

    # ---------------------------------------------------------- clusters
    def cluster(self, st):
        pos, kw = st.positional(), st.kwargs()
        flags = {p.text for p in pos if isinstance(p, Name)}
        if st.body or st.target is not None:
            self.fail(st, f"`{st.op}` takes no block and binds no name")
        match st.op:
            case "fill":
                form = (
                    "fill A0|A1|B0|B1 <- ADDR n=ENTRIES [at=ENTRY] [peers=(UNIT, ...)]"
                    " | fill A0|A1|B0|B1 recv n=ENTRIES"
                )
                if len(pos) == 2 and pos[1] == Name("recv") and set(kw) == {"n"}:
                    sel, bank = self.side(st, pos[0])
                    patches = []
                    n = self.field(st, kw["n"], "n", patches)
                    return C.Fill(
                        0, n, sel=sel, fbank=bank, recv=True, patches=tuple(patches)
                    )
                if len(pos) != 3 or pos[1] != Arrow("<-") or "n" not in kw:
                    self.fail(st, form)
                sel, bank = self.side(st, pos[0])
                patches = []
                addr = self.field(st, pos[2], "addr", patches)
                n = self.field(st, kw["n"], "n", patches)
                peers = ()
                if "peers" in kw:
                    t = kw["peers"]
                    items = t.items if isinstance(t, Tuple) else (t,)
                    peers = tuple(self.unit(st, u) for u in items)
                return C.Fill(
                    addr,
                    n,
                    sel=sel,
                    eoff=self.const(st, kw.get("at", Int(0))),
                    fbank=bank,
                    peers=peers,
                    patches=tuple(patches),
                )
            case "gemm":
                if not pos or not isinstance(pos[0], Dims) or len(pos[0].values) != 3:
                    self.fail(
                        st,
                        "gemm GMxGNxNK a=A0 b=B0 [acc] [aoff=] [boff=] [anchor=] "
                        "[emit=ADDR | emit to=UNIT l1=GRANULE [buf=] [count] [credit]]",
                    )
                if flags - {"acc", "emit", "count", "credit", "signal"}:
                    self.fail(
                        st,
                        f"gemm flags: acc emit count credit signal; got {sorted(flags)}",
                    )
                acc = "acc" in flags or bool(self.const(st, kw.get("acc", Int(0))))
                gm, gn, nk = pos[0].values
                sa, abank = self.side(st, kw.get("a", Name("A0")))
                sb, bbank = self.side(st, kw.get("b", Name("B0")))
                if (sa, sb) != (0, 1):
                    self.fail(st, "a= names an A bank, b= a B bank")
                emit = "emit" in kw or "emit" in flags
                patches = []
                to = None
                if "emit" in flags:
                    if "to" not in kw:
                        self.fail(
                            st, "gemm ... emit to=UNIT l1=GRANULE: emit names its sink"
                        )
                    to = self.drain(st, [], kw, (flags - {"acc", "emit"}) | {"fused"})
                    addr = to.addr
                else:
                    addr = self.field(st, kw["emit"], "addr", patches) if emit else 0
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
                    addr=addr,
                    anchor=self.const(st, kw.get("anchor", Int(C.ISA.cfg.anchor))),
                    to=to,
                    patches=tuple(patches),
                )
                try:
                    g.flits()
                except ValueError as e:
                    self.fail(st, str(e))
                return g
            case "drain":
                if "n" not in kw:
                    self.fail(st, "drain ... n=SUBTILES")
                return self.drain(st, pos, kw, flags)
            case "post":
                if pos or set(kw) != {"n", "to"}:
                    self.fail(st, "post n=CREDITS to=UNIT")
                mesh, dst = self.target(st, kw["to"])
                if mesh != self.p.machine.default:
                    self.fail(st, "a POST is a CU_SIGNAL inside this mesh")
                return C.Post(dst, self.const(st, kw["n"]))
        self.fail(st, f"a cluster's send holds fill, gemm, drain, post; not {st.op!r}")

    def drain(self, st, pos, kw, flags):
        if flags - {"fused", "signal", "count", "credit"}:
            self.fail(
                st,
                "drain ADDR n=N [fused] [mesh=M] | drain n=N to=UNIT l1=GRANULE "
                "[buf=0|1|2] [signal] [count] [credit] [ack=UNIT]",
            )
        patches = []
        n = self.field(st, kw["n"], "n", patches) if "n" in kw else 0
        if "to" not in kw and {"count", "credit"} & flags:
            self.fail(st, "count and credit are a drain to a unit's (to=UNIT)")
        if "to" not in kw:
            addrs = [p for p in pos if not isinstance(p, Name)]
            if len(addrs) != 1:
                self.fail(st, "drain ADDR n=N [fused] [mesh=M]")
            addr = self.field(st, addrs[0], "addr", patches)
            far = {}
            if "mesh" in kw:
                mesh = self.const(st, kw["mesh"])
                far = {"mesh": mesh, "fin": self.port(st, mesh)}
            return C.Drain(
                addr, n, fuse="fused" in flags, patches=tuple(patches), **far
            )
        if kw["to"] == Name("node"):
            # The node processor's coordinate: its RX queue takes the burst.
            mesh, dst = self.p.machine.default, NODE
        else:
            mesh, dst = self.target(st, kw["to"])
        dflags = (
            self.const(st, kw.get("flags", Int(0)))
            | (C.DFLAG_SIGNAL if "signal" in flags else 0)
            | (C.DFLAG_COUNT if "count" in flags else 0)
        )
        ack = self.coord(st, kw["ack"]) if "ack" in kw else (0, 0)
        here = self.p.machine.default
        far = {}
        if mesh != here:
            # The routers see a flit to this mesh's memory port; the far unit
            # rides in `fin`. A remote burst's ack must name its sink.
            far = {"mesh": mesh, "fin": dst}
            dst = self.port(st, here)
            if ack == (0, 0):
                ack = self.p.machine.mesh(mesh).agent or (0, 0)
        return C.Drain(
            32 * self.const(st, kw.get("l1", Int(0))),
            n,
            fuse="fused" in flags,
            dst=dst,
            dflags=dflags,
            ack=ack,
            buf=self.const(st, kw.get("buf", Int(C.BUF_L1A))),
            credit="credit" in flags,
            patches=tuple(patches),
            **far,
        )

    def field(self, st, t, name: str, patches: list) -> int:
        """A cluster field's constant; a register adds a patch."""
        c, reg = self.val(st, t)
        if reg is not None:
            patches.append(Patch(0, C.segments(name), reg, c))
        return c

    def port(self, st, mesh: int):
        m = self.p.machine.mesh(mesh)
        if not m.mem_ports:
            self.fail(st, f"mesh {mesh} names no memory port to cross by")
        return m.mem_ports[0]

    def collect(self, coord, st, op) -> None:
        """A fused DRAIN takes the sub-tiles an emitting GEMM hands out, in
        order: one without an emit before it waits forever."""
        pending = self.emits.setdefault(coord, [])
        if isinstance(op, C.Gemm) and op.emit:
            sink = op.to.node_fields() if op.to is not None else {}
            pending.append((op.addr, op.gm * op.gn, sink, st))
        elif isinstance(op, C.Drain) and op.fuse:
            if not pending:
                self.fail(
                    st,
                    "a fused drain collects an emitting gemm's sub-tiles; none is open",
                )
            addr, n, sink, _ = pending.pop(0)
            # The DRAIN re-latches the sink while the bursts are still leaving.
            if (op.addr, op.n, op.node_fields()) != (addr, n, sink):
                self.fail(
                    st, f"the open emit streams {n} sub-tiles to {addr:#x} {sink or ''}"
                )

    def finish(self) -> None:
        for pending in self.emits.values():
            if pending:
                self.fail(pending[0][3], "an emitting gemm no fused drain collects")

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
        return self.target(st, t)[1]

    # ------------------------------------------------------------- mover
    def move(self, st) -> None:
        pos = st.positional()
        posted = pos == [Name("posted")]
        if (pos and not posted) or not st.body:
            self.fail(st, "move [posted] (a block of mover ops)")
        if posted != (st.target is not None):
            self.fail(st, "t = move posted (a posted move's token), or move")
        ops = [self.mover_op(s) for s in st.body]
        if posted:
            self.tokens[st.target] = ("moves", self.p.post(*ops))
        else:
            self.p.move(*ops)

    def mover_op(self, st):
        pos, kw = st.positional(), st.kwargs()
        flags = {p.text for p in pos if isinstance(p, Name)}
        pos = [p for p in pos if not isinstance(p, Name)]
        legacy = {"copy": "bytes", "quant": "entries", "gt4": "groups"}.get(st.op)
        if legacy and (legacy in kw or (pos and not isinstance(pos[0], Call))):
            if len(pos) != 3 or pos[1] != Arrow("->") or set(kw) != {legacy}:
                self.fail(st, f"{st.op} SRC -> DST {legacy}=N [bpack], or WALK -> WALK")
            src, dst, n = (
                self.const(st, pos[0]),
                self.const(st, pos[2]),
                self.const(st, kw[legacy]),
            )
            if st.op == "quant":
                return M.Quantise(src, dst, n, b="bpack" in flags)
            return {"copy": M.Copy, "gt4": M.Transpose4}[st.op](src, dst, n)
        regs: list = []
        fix: list = []
        try:
            ew = EW[self.const(st, kw.get("ew", Int(32)))]
        except KeyError:
            self.fail(st, "ew= 8, 16 or 32")
        fl = PM.flag_byte(
            "nocoal" not in flags, self.const(st, kw.get("burst", Int(0)))
        )
        common = {"ewidth": ew, "flags": fl}
        match st.op:
            case "copy" | "xform" | "quant" | "gt4":
                if len(pos) != 3 or pos[1] != Arrow("->"):
                    self.fail(st, f"{st.op} WALK -> WALK")
                src, sfix = self.walk(st, pos[0], "src")
                dst, dfix = self.walk(st, pos[2], "dst")
                xform = (0, 0)
                mode = PM.COPY
                if st.op != "copy":
                    mode = PM.XFORM
                    xid = {"quant": M.XF_QUANT, "gt4": M.XF_GT4}.get(st.op)
                    if xid is None:
                        xid = self.const(st, kw["id"])
                    xmode = self.const(st, kw.get("mode", Int(int("bpack" in flags))))
                    xform = (xid, xmode)
                    common["ewidth"] = PM.W16
                imm = self.val(st, kw["pad"]) if "pad" in kw else None
                regs = PM.move(
                    mode,
                    src,
                    dst,
                    xform=xform,
                    imm=None if imm is None else imm[0],
                    **common,
                )
                fix = (
                    sfix
                    + dfix
                    + ([("imm", imm[1], imm[0])] if imm and imm[1] is not None else [])
                )
            case "fill" | "gen":
                if len(pos) != 1:
                    self.fail(st, f"{st.op} WALK value=V | seed=S")
                dst, fix = self.walk(st, pos[0], "dst")
                key = "value" if st.op == "fill" else "seed"
                c, r = self.val(st, kw.get(key, Int(0)))
                regs = PM.move(
                    PM.FILL if st.op == "fill" else PM.GENERATE,
                    None,
                    dst,
                    **({"imm": c} if st.op == "fill" else {"seed": c}),
                    **common,
                )
                if r is not None:
                    fix.append(("imm" if st.op == "fill" else "seed", r, c))
            case "gather" | "scatter":
                if (
                    len(pos) != 3
                    or pos[1] != Arrow("->")
                    or not {"index", "n", "pitch", "words"} <= set(kw)
                ):
                    self.fail(
                        st, f"{st.op} SRC -> DST index=ADDR n=N pitch=P words=W [fp]"
                    )
                ic, ireg = self.val(st, kw["index"])
                n, nreg = self.val(st, kw["n"])
                pitch, preg = self.val(st, kw["pitch"])
                words, wreg = self.val(st, kw["words"])
                if st.op == "gather":
                    base, bfix = self.base(st, pos[0], "src")
                    walk, wfix = self.walk(st, pos[2], "dst")
                    regs = PM.move(
                        PM.GATHER,
                        (base, [(1, 32)]),
                        walk,
                        index=(ic, max(n, 1)),
                        row=(pitch, max(words, 1)),
                        fp="fp" in flags,
                        **common,
                    )
                else:
                    walk, wfix = self.walk(st, pos[0], "src")
                    base, bfix = self.base(st, pos[2], "dst")
                    regs = PM.move(
                        PM.SCATTER,
                        walk,
                        (base, [(1, 32)]),
                        index=(ic, max(n, 1)),
                        row=(pitch, max(words, 1)),
                        fp="fp" in flags,
                        **common,
                    )
                fix = bfix + wfix
                fix += [
                    (k, r, c)
                    for k, r, c in (
                        ("index", ireg, ic),
                        ("count", nreg, n),
                        ("pitch", preg, pitch),
                        ("words", wreg, words),
                    )
                    if r is not None
                ]
            case _:
                self.fail(
                    st,
                    f"a move holds copy quant gt4 xform fill gen gather scatter; not {st.op!r}",
                )
        return M.Move(tuple(regs), tuple(self.mover_patches(st, regs, fix)))

    def mover_patches(self, st, regs, fix) -> list:
        """Patches for `fix` (``(field, register, constant)``) in `regs`."""
        where = PM.fields(regs)
        key = {
            "imm": (PM.R_IMM, "imm"),
            "seed": (PM.R_SEED, "seed"),
            "index": (PM.R_INDEX, "index"),
            "count": (PM.R_INDEX, "count"),
            "pitch": (PM.R_ROW, "pitch"),
            "words": (PM.R_ROW, "words"),
        }
        out = []
        for name, reg, c in fix:
            k = key.get(name) or (
                (PM.R_HDR, name) if name.endswith("base") else (PM.R_DIM, name)
            )
            if k not in where:
                self.fail(st, f"no mover field {name} to set from a register")
            i, bit, width = where[k]
            out.append(Patch(i, ((bit, width, 0),), reg, c))
        return out

    def base(self, st, t, side: str) -> tuple:
        c, reg = self.val(st, t)
        return c, ([(f"{side}.base", reg, c)] if reg is not None else [])

    def walk(self, st, t, side: str) -> tuple:
        """`w(BASE, (C, S[, AXIS, ASTEP]), ..., win1=(B, E), win2=(B, E))` ->
        ``(Walk, fixes)``; a base or a count may be a register."""
        if not isinstance(t, Call) or t.name != "w" or not t.args:
            self.fail(st, f"wanted a walk w(BASE, (COUNT, STRIDE), ...), got {t!r}")
        args = [a for a in t.args if not isinstance(a, Kw)]
        kws = {a.name: a.value for a in t.args if isinstance(a, Kw)}
        base, fix = self.base(st, args[0], side)
        dims = []
        for i, d in enumerate(args[1:]):
            if not isinstance(d, Tuple) or len(d.items) not in (2, 4):
                self.fail(
                    st,
                    f"a level is (COUNT, STRIDE) or (COUNT, STRIDE, AXIS, ASTEP), got {d!r}",
                )
            c, reg = self.val(st, d.items[0])
            if reg is not None:
                fix.append((f"{side}.count{i}", reg, c))
            dims.append((c, *(self.const(st, x) for x in d.items[1:])))
        if not dims:
            self.fail(st, "a walk has at least one level")
        wins = []
        for k in ("win1", "win2"):
            w = kws.get(k)
            if w is None:
                wins.append((0, 0))
            elif isinstance(w, Tuple) and len(w.items) == 2:
                wins.append(tuple(self.const(st, x) for x in w.items))
            else:
                self.fail(st, f"{k}=(BASE, EXTENT)")
        return PM.Walk(base, tuple(dims), tuple(wins)), fix

    # ------------------------------------------------------------- waits
    def wait(self, st) -> None:
        pos, kw = st.positional(), st.kwargs()
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
        if pos and pos[0] == Name("slot"):
            if (
                len(pos) not in (2, 3)
                or "n" not in kw
                or (len(pos) == 3 and pos[2] != Name("consume"))
            ):
                self.fail(st, "wait slot S n=N [consume]")
            self.p.wait_slot(
                self.const(st, pos[1]), self.const(st, kw["n"]), len(pos) == 3
            )
            return
        if pos and pos[0] == Name("bell"):
            if len(pos) != 2:
                self.fail(st, "wait bell MESH [n=N]")
            self.p.wait_bell(
                self.const(st, pos[1]), self.const(st, kw.get("n", Int(1)))
            )
            return
        coord = self.unit(st, self.one(st))
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
        c, reg = self.val(st, pos[2])
        patches = [] if reg is None else [Patch(0, ((64, 40, 0),), reg, c)]
        self.p.fetch(coord, c, self.const(st, st.kwargs()["n"]), patches)

    # ------------------------------------------------------------ terms
    def one(self, st):
        pos = st.positional()
        if len(pos) != 1:
            self.fail(st, f"`{st.op}` takes one operand")
        return pos[0]

    def target(self, st, t) -> tuple:
        """``(mesh, coord)`` of a unit: `vc[i]`, `mg[i]`, or `meshM.mg[i]`."""
        if isinstance(t, View) and len(t.slices) == 1:
            prefix, _, kind = t.name.rpartition(".")
            mesh = self.p.machine.default
            if prefix:
                if not (prefix.startswith("mesh") and prefix[4:].isdigit()):
                    self.fail(
                        st, f"a unit in another mesh is meshM.{kind}[i], got {t.name}"
                    )
                mesh = int(prefix[4:])
            if kind in UNITS:
                i = self.const(st, t.slices[0])
                have = sorted(self.p.machine.coords(UNITS[kind], mesh))
                if not 0 <= i < len(have):
                    self.fail(
                        st,
                        f"{t.name}[{i}]: this machine has {len(have)} in mesh {mesh}",
                    )
                return mesh, have[i]
        self.fail(st, f"wanted a unit vc[i] / mg[i] / meshM.mg[i], got {t!r}")

    def unit(self, st, t) -> tuple:
        mesh, coord = self.target(st, t)
        if mesh != self.p.machine.default:
            self.fail(
                st, f"{t!r} is in mesh {mesh}: a node sends only into its own (H1)"
            )
        return coord

    def const(self, st, t) -> int:
        if isinstance(t, Int):
            return t.value
        self.fail(st, f"wanted a constant (parameters are bound), got {t!r}")


def _bodies(module, name: str, binds: dict) -> tuple:
    body = module.body(name, "l1")
    r = module.reader()
    missing = [p for p, _ in body.params if p not in binds]
    if missing:
        raise ValueError(f"{name}.l1: unbound parameters {missing}")
    macros = {m.name: (m.params, m.stmts) for m in module.macros.values()}
    stmts = Expander(macros, r.fail, unroll_all=True).stmts(body.stmts, dict(binds))
    nodes = [s for s in stmts if s.op == "node"]
    if not nodes:
        return r, {None: stmts}
    if len(nodes) != len(stmts):
        r.fail(
            next(s for s in stmts if s.op != "node"),
            "with `node` blocks, every statement is in one",
        )
    out = {}
    for s in nodes:
        pos = s.positional()
        if len(pos) != 1 or not isinstance(pos[0], Int):
            r.fail(s, "node N (a block)")
        if pos[0].value in out:
            r.fail(s, f"node {pos[0].value} twice")
        out[pos[0].value] = s.body
    return r, out


def programs(module, name: str, binds: dict, machine, entry=vector.UNKNOWN) -> dict:
    """``{node: Program}`` of `fn name.l1`, each parameter bound to an address:
    one per `node N` block, on `machine` seen from mesh N; ``{None: ...}``
    for a body with no node block."""
    _, bodies = _bodies(module, name, binds)
    out = {}
    for node, stmts in bodies.items():
        m = machine if node is None else machine.for_mesh(node)
        p = Program(m)
        reader = NodeReader(module, p, entry)
        reader.stmts(stmts)
        reader.finish()
        out[node] = p
    return out


def program(module, name: str, binds: dict, machine, entry=vector.UNKNOWN) -> Program:
    """`fn name.l1` with each parameter bound to an address (one node)."""
    got = programs(module, name, binds, machine, entry)
    if list(got) != [None]:
        raise ValueError(f"{name}.l1 has node blocks {sorted(got)}: use programs()")
    return got[None]


__all__ = ["NodeReader", "program", "programs"]
