"""The V2 vector core's L1 text (every form in
docs/projects/kohakutpu/ir/ktpu.md §3): an `image` body to IMEM words.

CHKVL / XB / USTR / PSTR are not written: the encoder tracks the core's
configuration and emits one where an op needs a value the core does not hold;
a field an op does not read keeps the value in force. Config persists across
RUNs (`v2_core.v` resets it only at core reset), so an image enters in an
unknown state unless the caller states one. A `loop` body enters in the state
its own end leaves, set up once before the loop.
"""

from dataclasses import dataclass, replace

from kohakuaccel.text.meta import Expander
from kohakuaccel.text.syntax import (
    AddAssign,
    Arrow,
    Assign,
    Call,
    Float,
    Int,
    Kw,
    Name,
    Slice,
    Tuple,
    View,
)
from kohakutpu.hw import vector2 as A

# ----------------------------------------------------------------- the ISA
UNARY = {0x00, 0x01, 0x02, 0x0E, 0x0F, 0x10, 0x11}
READS_B = {0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x12}
READS_C = {0x03, 0x04, 0x06, 0x07, 0x0A}
COMPARE = {0x0B, 0x0C, 0x0D}
MATH = {k.lower(): v for k, v in A.OPS.items()}

WAITERS = {"fe": A.W_FE, "unpack": A.W_U, "math": A.W_M, "pack": A.W_P}
WAITERS |= {"fill": A.W_A, "drain": A.W_D}
KINDS = {"fill": A.K_FILL, "unpack": A.K_UNPACK, "math": A.K_MATH, "pack": A.K_PACK}
KINDS |= {"drain": A.K_DRAIN, "unpack1": A.K_UNPACK1, "pack1": A.K_PACK1}
MODES = {"f16": A.F16, "gt4": A.GT4, "f32": A.F32}
XMODES = {"xor": A.XM_XOR, "rot": A.XM_ROT, "bcast": A.XM_BCAST, "merge": A.XM_MERGE}
POINTERS = {"uptr": A.CFG_UPTR, "uinc": A.CFG_UINC}
POINTERS |= {"pptr": A.CFG_PPTR, "pinc": A.CFG_PINC}
FLAT = (1, 4)
DEF_NIB = (0, 1)


@dataclass(frozen=True)
class State:
    """The core's configuration as far as the encoder knows it; None is
    unknown. `chk` holds the (base, stride) of a, b, c, d; `xb` is
    (xm, xk, sh, xc, pm, pr)."""

    vl: int | None
    chk: tuple
    xb: tuple
    ustr: tuple | None
    pstr: tuple | None


UNKNOWN = State(None, (None,) * 4, (None,) * 6, None, None)
#: After core reset (`v2_core.v`): VL 128, CHK 0x1111, XB 0, flat walks.
RESET = State(128, (DEF_NIB,) * 4, (0,) * 6, FLAT, FLAT)


def meet(a: State, b: State) -> State:
    """The fields two paths agree on."""

    def one(x, y):
        return x if x == y else None

    return State(
        one(a.vl, b.vl),
        tuple(one(x, y) for x, y in zip(a.chk, b.chk, strict=True)),
        tuple(one(x, y) for x, y in zip(a.xb, b.xb, strict=True)),
        one(a.ustr, b.ustr),
        one(a.pstr, b.pstr),
    )


@dataclass(frozen=True)
class Src:
    """A math source: register `reg` of `sel` (V/S/K), its chunk select,
    and a crossbar `(xm, xk, sh)` on it."""

    reg: int
    sel: int
    nib: tuple | None = None
    xbar: tuple | None = None


# ----------------------------------------------------------------- encoder
class Encoder:
    """Encodes one image body (statements already expanded) to words.

    `fail(st, message)` reports at a statement. `config` counts the CHKVL /
    XB / USTR / PSTR words the encoder chose, for reports."""

    def __init__(self, fail, entry: State = UNKNOWN) -> None:
        self.fail = fail
        self.entry = entry
        self.config = 0

    def encode(self, stmts: list) -> list:
        words, _ = self.block(stmts, self.entry)
        return words

    # ------------------------------------------------------------ blocks
    def block(self, stmts: list, state: State, depth: int = 0) -> tuple:
        out: list = []
        for st in stmts:
            words, state = self.stmt(st, state, depth)
            out += words
        return out, state

    def stmt(self, st, state: State, depth: int) -> tuple:
        if st.target is not None:
            self.fail(st, f"an image statement binds no name ({st.target} = ...)")
        if st.body and st.op not in ("loop", "skipz"):
            self.fail(st, f"`{st.op}` takes no block")
        op = st.op
        if op in MATH:
            return self.math(st, state)
        if op == "loop":
            return self.loop(st, state, depth)
        if op == "skipz":
            return self.skipz(st, state, depth)
        if op in ("vunpk", "vpack"):
            return self.walk(st, state)
        handler = getattr(self, "_" + op, None)
        if handler is None:
            self.fail(st, f"no V2 instruction {op!r}")
        try:
            got = handler(st)
        except A.VectorEncodeError as e:
            self.fail(st, str(e))
        if op in ("vcfg", "word"):
            return got, UNKNOWN
        if op == "setvl":
            return got, replace(state, vl=None)
        return got, state

    def loop(self, st, state: State, depth: int) -> tuple:
        sreg = self._sreg(st, self._one(st, "loop sN"))
        if depth:
            self.fail(st, "a loop inside a loop: VLOOP does not nest")
        if not st.body:
            self.fail(st, "an empty loop")
        # Entry as found, or the state the body's end leaves (set up once
        # before): the shorter body wins, then the shorter set-up.
        best = None
        for start, hoist in (
            (state, False),
            (self._quiet(st.body, UNKNOWN, depth + 1), True),
        ):
            entry = start
            for _ in range(16):
                nxt = meet(entry, self._quiet(st.body, entry, depth + 1))
                if nxt == entry:
                    break
                entry = nxt
            n = self.config
            pre = self.setup(state, entry)[0] if hoist else []
            body, _ = self.block(st.body, entry, depth + 1)
            key = (len(body), len(pre))
            if best is None or key < best[0]:
                best = (key, pre, body, entry, self.config)
            self.config = n
        _, pre, body, entry, self.config = best
        try:
            head = A.vloop(sreg, len(body))
        except A.VectorEncodeError as e:
            self.fail(st, str(e))
        return pre + [head] + body, entry

    def skipz(self, st, state: State, depth: int) -> tuple:
        sreg = self._sreg(st, self._one(st, "skipz sN"))
        body, end = self.block(st.body, state, depth)
        try:
            head = A.vskipz(sreg, len(body))
        except A.VectorEncodeError as e:
            self.fail(st, str(e))
        return [head] + body, meet(state, end)

    def _quiet(self, stmts, state, depth) -> State:
        """The end state of a trial encoding, whose words are not counted."""
        n = self.config
        _, end = self.block(stmts, state, depth)
        self.config = n
        return end

    def setup(self, state: State, want: State) -> tuple:
        """Words taking the core from `state` to the known fields of `want`."""
        out = []
        chk = tuple(
            w if w is not None else (s if s is not None else DEF_NIB)
            for w, s in zip(want.chk, state.chk, strict=True)
        )
        vl_moves = want.vl is not None and want.vl != state.vl
        chk_moves = any(
            w is not None and w != s for w, s in zip(want.chk, state.chk, strict=True)
        )
        if vl_moves or chk_moves:
            vl = want.vl if want.vl is not None else state.vl
            out.append(A.chkvl(vl, *chk) if vl is not None else A.chk(*chk))
            state = replace(state, vl=vl, chk=chk)
        if any(
            w is not None and w != s for w, s in zip(want.xb, state.xb, strict=True)
        ):
            xb = tuple(
                w if w is not None else (s if s is not None else 0)
                for w, s in zip(want.xb, state.xb, strict=True)
            )
            out.append(A.xb(*xb))
            state = replace(state, xb=xb)
        for kind, sel in (("ustr", A.CFG_USTR), ("pstr", A.CFG_PSTR)):
            w = getattr(want, kind)
            if w is not None and w != getattr(state, kind):
                out.append(A.cfg_stride(sel, *w))
                state = replace(state, **{kind: w})
        self.config += len(out)
        return out, state

    # -------------------------------------------------------------- math
    def math(self, st, state: State) -> tuple:
        code = MATH[st.op]
        pos = st.positional()
        kw = st.kwargs()
        fields = (
            ["a"]
            + (["b"] if code in READS_B else [])
            + (["c"] if code in READS_C else [])
        )
        if len(pos) != 1 + len(fields):
            self.fail(st, f"{st.op} d, {', '.join(fields)}: {1 + len(fields)} operands")
        compare = code in COMPARE
        dreg, dnib = self._dest(st, pos[0], compare)
        srcs = dict(zip(fields, (self._src(st, t) for t in pos[1:]), strict=True))
        reg = {"a": (0, A.SRC_V), "b": (0, A.SRC_V), "c": (0, A.SRC_V)}
        nib = {"a": None, "b": None, "c": None, "d": dnib or DEF_NIB}
        xbar, xc = None, 0
        for f, s in srcs.items():
            if s.xbar is not None:
                if f == "a":
                    self.fail(st, "the crossbar is on b (and c with b's input)")
                if xbar is not None and (xbar, reg["b"]) != (s.xbar, (s.reg, s.sel)):
                    self.fail(
                        st, "c through the crossbar reads b's input: name it twice"
                    )
                xbar = s.xbar
                if f == "c":
                    xc = 1
                reg["b"] = (s.reg, s.sel)
                nib["b"] = s.nib or DEF_NIB
                if f == "c":
                    reg["c"] = (s.reg, s.sel)
                continue
            reg[f] = (s.reg, s.sel)
            if s.sel == A.SRC_V:
                nib[f] = s.nib or DEF_NIB
        if xc and "b" in srcs and srcs["b"].xbar is None:
            self.fail(st, "c through the crossbar while b is not: b is the crossbar's")
        pm, pr = self._pred(st, kw)
        if compare:
            if pm:
                self.fail(st, "a compare writes its predicate unpredicated")
            pr = dreg
            dreg = 0
        selects = any(s.nib is not None for s in srcs.values()) or dnib is not None
        om = selects or xbar is not None or pm != 0 or compare
        vl = self._vl(st, kw)
        words = []
        if om:
            xm, xk, sh = xbar if xbar is not None else (A.XM_NONE, None, None)
            want_xb = (
                xm,
                xk,
                sh if xm == A.XM_BCAST else None,
                xc if "c" in srcs else None,
                pm,
                pr if (pm or compare) else None,
            )
            want = State(
                vl, (nib["a"], nib["b"], nib["c"], nib["d"]), want_xb, None, None
            )
        else:
            want = State(vl, (None,) * 4, (None,) * 6, None, None)
        pre, state = self.setup(state, want)
        words += pre
        (va, sa), (vb, sb), (vc, sc) = reg["a"], reg["b"], reg["c"]
        try:
            words.append(A.math(code, dreg, va, vb, vc, sa, sb, sc, om=om))
        except A.VectorEncodeError as e:
            self.fail(st, str(e))
        return words, state

    def _vl(self, st, kw):
        v = kw.get("vl")
        if v is None:
            return 128
        if v == Name("keep"):
            return None
        n = self._int(st, v)
        if not 1 <= n <= 128:
            self.fail(st, f"vl={n} is not 1..128")
        return n

    def _pred(self, st, kw):
        got = [(k, kw[k]) for k in ("if", "unless") if k in kw]
        if not got:
            return 0, 0
        if len(got) > 1:
            self.fail(st, "one of if= / unless=")
        k, t = got[0]
        return (1 if k == "if" else 2), self._numbered(st, t, "p", 4)

    def _dest(self, st, t, compare: bool):
        prefix = "p" if compare else "v"
        limit = 4 if compare else A.REGS
        if isinstance(t, View):
            return self._numbered(st, Name(t.name), prefix, limit), self._nib(st, t)
        return self._numbered(st, t, prefix, limit), None

    def _src(self, st, t) -> Src:
        if isinstance(t, Call) and t.name in XMODES:
            if not t.args:
                self.fail(st, f"{t.name}(REGISTER, ...)")
            inner = self._src(st, t.args[0])
            if inner.sel != A.SRC_V or inner.xbar is not None:
                self.fail(st, f"{t.name} takes a vector register")
            pos = [a for a in t.args[1:] if not isinstance(a, Kw)]
            kw = {a.name: a.value for a in t.args[1:] if isinstance(a, Kw)}
            xm = XMODES[t.name]
            if xm == A.XM_BCAST:
                if pos:
                    self.fail(st, "bcast(REG, lane=L, sh=S)")
                xk = self._int(st, kw.get("lane", Int(0)))
                sh = self._int(st, kw.get("sh", Int(0)))
            else:
                if len(pos) != 1 or kw:
                    self.fail(st, f"{t.name}(REG, K)")
                xk, sh = self._int(st, pos[0]), 0
            return replace(inner, xbar=(xm, xk, sh))
        if isinstance(t, View):
            src = self._src(st, Name(t.name))
            if src.sel != A.SRC_V:
                self.fail(st, "a chunk select is on a vector register")
            return replace(src, nib=self._nib(st, t))
        if isinstance(t, Name) and len(t.text) > 1 and t.text[1:].isdigit():
            kind, n = t.text[0], int(t.text[1:])
            limit = {"v": A.REGS, "s": 16, "k": 4}.get(kind)
            if limit is not None:
                if n >= limit:
                    self.fail(st, f"{t.text}: {kind}0..{kind}{limit - 1}")
                return Src(n, {"v": A.SRC_V, "s": A.SRC_S, "k": A.SRC_K}[kind])
        self.fail(st, f"wanted vN, sN, kN, a chunk select or a crossbar, got {t!r}")

    def _nib(self, st, t: View) -> tuple:
        if len(t.slices) != 1:
            self.fail(
                st, f"{t.name}[c:] (from chunk c) or {t.name}[c] (chunk c every beat)"
            )
        (s,) = t.slices
        if isinstance(s, Slice) and s.hi is None and s.lo is not None:
            base, stride = self._int(st, s.lo), 1
        elif isinstance(s, Int):
            base, stride = s.value, 0
        else:
            self.fail(st, f"{t.name}[c:] or {t.name}[c], got {s!r}")
        if not 0 <= base < A.CHUNKS:
            self.fail(st, f"chunk {base}: 0..{A.CHUNKS - 1}")
        return (base, stride)

    # ----------------------------------------------------- unpack / pack
    def walk(self, st, state: State) -> tuple:
        pos = st.positional()
        kw = st.kwargs()
        unpack = st.op == "vunpk"
        arrow = Arrow("<-" if unpack else "->")
        flags = {p.text for p in pos[4:] if isinstance(p, Name)}
        if len(pos) < 4 or pos[1] != arrow or not isinstance(pos[3], Name):
            self.fail(st, f"{st.op} vN {arrow.text} l1[OFF] MODE ...")
        if flags - {"rel"} or len(pos) - 4 != len(flags):
            self.fail(st, f"{st.op} takes the flag rel; got {pos[4:]}")
        reg, nib = self._dest(st, pos[0], False)
        off = self._l1(st, pos[2])
        mode = pos[3].text
        q = self._int(st, kw.get("q", Int(0)))
        rel = "rel" in flags
        walk = self._pair(st, kw["walk"]) if "walk" in kw else FLAT
        kind = "ustr" if unpack else "pstr"
        want = replace(UNKNOWN, **{kind: walk})
        pre, state = self.setup(state, want)
        try:
            if mode in ("mx7a", "mx7b"):
                if unpack or nib is not None or "n" in kw:
                    self.fail(
                        st,
                        "an MX7 pack takes a whole register: vpack vN -> l1[OFF] mx7a",
                    )
                word = A.vpack_mx7(reg, off, mode == "mx7b", rel, q)
            else:
                if mode not in MODES:
                    self.fail(
                        st, f"mode {mode}: {', '.join(MODES)} (packs also mx7a mx7b)"
                    )
                if nib is not None and nib[1] != 1:
                    self.fail(st, "a walk starts at a chunk: vN[c:]")
                cb = nib[0] if nib else 0
                n = self._int(st, kw.get("n", Int(A.CHUNKS - cb)))
                fn = A.vunpk if unpack else A.vpack
                word = fn(reg, off, n, MODES[mode], cb, rel, q)
        except A.VectorEncodeError as e:
            self.fail(st, str(e))
        return pre + [word], state

    # -------------------------------------------------------- the rest
    def _vfill(self, st):
        ad, off, flags = self._dma(st, "->", ())
        return [A.vfill(ad, off, "rel" in flags)]

    def _vdrain(self, st):
        ad, off, flags = self._dma(st, "<-", ("signal",))
        kw = st.kwargs()
        if "to" not in kw:
            if "buf" in kw or "signal" in flags:
                self.fail(st, "buf= and signal are a peer drain's (to=(x, y))")
            return [A.vdrain(ad, off, "rel" in flags)]
        x, y = self._pair(st, kw["to"])
        buf = self._int(st, kw.get("buf", Int(0)))
        return [
            A.vdrain(
                ad,
                off,
                "rel" in flags,
                node=(x, y),
                buf_id=buf,
                signal="signal" in flags,
            )
        ]

    def _dma(self, st, arrow, extra):
        pos = st.positional()
        if len(pos) < 3 or pos[1] != Arrow(arrow):
            self.fail(st, f"{st.op} aN {arrow} l1[OFF] [rel]")
        flags = [p.text for p in pos[3:] if isinstance(p, Name)]
        if len(flags) != len(pos) - 3 or set(flags) - {"rel", *extra}:
            self.fail(st, f"{st.op} flags: rel {' '.join(extra)}")
        return self._numbered(st, pos[0], "a", 8), self._l1(st, pos[2]), set(flags)

    def _vsync(self, st):
        pos = st.positional()
        kw = st.kwargs()
        if len(pos) != 1 or not isinstance(pos[0], Name) or pos[0].text not in WAITERS:
            self.fail(st, f"vsync WAITER ...: {', '.join(WAITERS)}")
        waiter = WAITERS[pos[0].text]
        q = self._int(st, kw.get("q", Int(0)))
        if ("on" in kw) == ("mark" in kw):
            self.fail(st, "vsync waits on=KIND or mark=mN")
        if "mark" in kw:
            if "slack" in kw:
                self.fail(st, "a mark's slack is the vmark's")
            return [A.vsync(waiter, mark=self._numbered(st, kw["mark"], "m", 8), q=q)]
        slack = self._int(st, kw.get("slack", Int(0)))
        return [A.vsync(waiter, self._kind(st, kw["on"]), slack, q=q)]

    def _vmark(self, st):
        pos = st.positional()
        kw = st.kwargs()
        if len(pos) != 1 or "on" not in kw or set(kw) - {"on", "slack"}:
            self.fail(st, "vmark mN on=KIND [slack=N]")
        mark = self._numbered(st, pos[0], "m", 8)
        if "on" not in kw:
            self.fail(st, "vmark mN on=KIND [slack=N]")
        return [
            A.vmark(
                mark, self._kind(st, kw["on"]), self._int(st, kw.get("slack", Int(0)))
            )
        ]

    def _vbar(self, st):
        if st.positional():
            self.fail(st, "vbar [q=1]")
        return [A.vbar(self._int(st, st.kwargs().get("q", Int(0))))]

    def _ainc(self, st):
        (a,) = st.args or [None]
        if not isinstance(a, AddAssign):
            self.fail(st, "ainc aN += BYTES")
        inc = self._int(st, a.rhs)
        if not -(1 << 17) <= inc < 1 << 17:
            self.fail(st, f"ainc {inc}: signed 18 bits")
        return [A.ainc(self._numbered(st, a.lhs, "a", 8), inc)]

    def _pointer(self, st):
        return [A.cfg_ptr(POINTERS[st.op], self._int(st, self._one(st, f"{st.op} N")))]

    _uptr = _uinc = _pptr = _pinc = _pointer

    def _seti(self, st):
        (a,) = st.args or [None]
        if not isinstance(a, Assign) or not isinstance(a.lhs, Name):
            self.fail(st, "seti sN = VALUE / seti k3 = VALUE")
        if a.lhs.text == "k3":
            sreg, to_k = 0, True
        else:
            sreg, to_k = self._numbered(st, a.lhs, "s", 16), False
        v = a.rhs
        if isinstance(v, Float):
            value = A.e8m15(v.value)
        elif isinstance(v, Int):
            value = v.value & 0xFFFFFF
        else:
            self.fail(st, f"seti takes a constant, got {v!r}")
        return [A.vseti(sreg, to_k), value]

    def _setvl(self, st):
        return [A.vsetvl(self._sreg(st, self._one(st, "setvl sN")))]

    def _getstk(self, st):
        return [A.getstk(self._sreg(st, self._one(st, "getstk sN")))]

    def _halt(self, st):
        if st.args:
            self.fail(st, "halt takes nothing")
        return [A.vhalt()]

    def _vcfg(self, st):
        pos = st.positional()
        if len(pos) != 2:
            self.fail(st, "vcfg SEL PAYLOAD")
        return [A.vcfg(self._int(st, pos[0]), self._int(st, pos[1]))]

    def _word(self, st):
        w = self._int(st, self._one(st, "word W"))
        if not 0 <= w < 1 << 32:
            self.fail(st, f"word {w:#x}: 32 bits")
        return [w]

    # ------------------------------------------------------------ terms
    def _one(self, st, form):
        pos = st.positional()
        if len(pos) != 1 or st.kwargs():
            self.fail(st, f"wanted `{form}`")
        return pos[0]

    def _int(self, st, t) -> int:
        if isinstance(t, Int):
            return t.value
        self.fail(st, f"wanted a constant integer, got {t!r}")

    def _pair(self, st, t) -> tuple:
        if not isinstance(t, Tuple) or len(t.items) != 2:
            self.fail(st, f"wanted (A, B), got {t!r}")
        return tuple(self._int(st, x) for x in t.items)

    def _numbered(self, st, t, prefix: str, limit: int) -> int:
        if isinstance(t, Name) and t.text.startswith(prefix) and t.text[1:].isdigit():
            n = int(t.text[1:])
            if n < limit:
                return n
        self.fail(st, f"wanted {prefix}0..{prefix}{limit - 1}, got {t!r}")

    def _sreg(self, st, t) -> int:
        return self._numbered(st, t, "s", 16)

    def _kind(self, st, t) -> int:
        if not isinstance(t, Name) or t.text not in KINDS:
            self.fail(st, f"on=KIND: {', '.join(KINDS)}")
        return KINDS[t.text]

    def _l1(self, st, t) -> int:
        if not isinstance(t, View) or t.name != "l1" or len(t.slices) != 1:
            self.fail(st, f"wanted l1[OFFSET], got {t!r}")
        return self._int(st, t.slices[0])


def desc(fail, st) -> dict:
    """`desc aN = BASE walk=(STRIDE, BOUND) | walk=((S, B), ...)` as
    ``{(ad, field): value}``. All four dims are written (an unused one as
    (0, 0), a bound of 0 reading as 1): a dim left out would keep whatever the
    last kernel on the core set."""
    rest = [x for x in st.args if not (isinstance(x, Assign) and x.lhs == Name("walk"))]
    lhs = rest[0].lhs if len(rest) == 1 and isinstance(rest[0], Assign) else None
    if not (isinstance(lhs, Name) and lhs.text[:1] == "a" and lhs.text[1:].isdigit()):
        fail(st, "desc aN = BASE walk=(STRIDE, BOUND) | walk=((S, B), ...)")
    ad = int(lhs.text[1:])
    if ad >= 8:
        fail(st, f"a{ad}: descriptors a0..a7")
    walk = st.kwargs().get("walk")
    if walk is None:
        pairs = ()
    elif (
        isinstance(walk, Tuple)
        and walk.items
        and all(isinstance(i, Tuple) for i in walk.items)
    ):
        pairs = walk.items
    else:
        pairs = (walk,)
    dims = []
    for p in pairs:
        if (
            not isinstance(p, Tuple)
            or len(p.items) != 2
            or not all(isinstance(x, Int) for x in p.items)
        ):
            fail(st, f"a dim is (STRIDE, BOUND) in constants, got {p!r}")
        dims.append(tuple(x.value for x in p.items))
    if len(dims) > 4:
        fail(st, f"{len(dims)} dims: a descriptor holds 4")
    if not isinstance(rest[0].rhs, Int):
        fail(st, f"a descriptor's base is a constant address, got {rest[0].rhs!r}")
    out = {(ad, 0): rest[0].rhs.value}
    for i in range(4):
        s, b = dims[i] if i < len(dims) else (0, 0)
        out[(ad, i + 1)] = A.dim(s, b)
    return out


def image(module, name: str, args: tuple, entry: State = UNKNOWN) -> tuple:
    """``(words, descs)`` of `module`'s image `name` for constant `args`: the
    IMEM words, and the descriptor fields its leading `desc` statements set
    (sent before the RUN, outside the image, so the words stay resident)."""
    r = module.reader()
    d = module.images.get(name)
    if d is None:
        raise KeyError(
            f"no image {name} in {module.file}; it has {sorted(module.images)}"
        )
    if len(args) != len(d.params):
        raise ValueError(
            f"image {name} takes {len(d.params)} arguments, got {len(args)}"
        )
    macros = {m.name: (m.params, m.stmts) for m in module.macros.values()}
    env = dict(zip(d.params, args, strict=True))
    stmts = Expander(macros, r.fail, unroll_all=True).stmts(d.stmts, env)
    descs: dict = {}
    while stmts and stmts[0].op == "desc":
        descs.update(desc(r.fail, stmts.pop(0)))
    for st in stmts:
        if st.op == "desc":
            r.fail(st, "an image's descriptors lead it: they are set before its RUN")
    return Encoder(r.fail, entry).encode(stmts), descs


__all__ = ["RESET", "UNKNOWN", "Encoder", "State", "image", "meet"]
