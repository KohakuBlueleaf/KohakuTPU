"""A Streaming Row Unit's `send` block in an `fn NAME.l1` body: the job's
configuration and the unit's instructions (docs/projects/kohakutpu/ir/ktpu.md
s2.4 is the statement table).

    send sr[0]
        job nw=8 nsr=4 out=f16 buf r1 row0=0
        pass 0
            fin max R0 <- R0, ST1
        pass 1 r2 emit
            op fn a=X k=R0 cs=3
            fin add R1 <- R1, ST2
        load table <- tab n=256
        run a=x out=y
        store rows -> stats n=16

`job` sets every global field (one left out is 0); a `pass` block sets its
pass and lays its `op`s into the op store and its `fin`s into the finalise
program, each pass after the last. `run` first writes the configuration words
that differ from what this program last wrote to the unit (all of them the
first time), then the RUN.
"""

from kohakutpu.compiler.encode import sru as E
from kohakutpu.compiler.isa import sru as S
from kohakutpu.language.text.syntax import Arrow, Float, Int, Name, Neg, View

#: `job` field names: G's, lowercase; 1-bit ones may be written as a flag.
JOB = {k.lower(): k for k in S.G}
FLAGS = {"buf", "use_b", "mask", "r1"}
#: E8M15-valued job fields, written as numbers.
E8_JOB = {"mval", "k0", "k1", "k2", "k3"}
OUT = {"none": 0, "f16": 1, "mx7a": 2, "mx7b": 3}
#: Finalise operand order per opcode, as written after the arrow.
FIN_ARGS = {
    "mov": ("A",),
    "add": ("A", "C"),
    "sub": ("A", "C"),
    "mul": ("A", "B"),
    "fma": ("A", "B", "C"),
    "max": ("A", "B"),
    "min": ("A", "B"),
    "rcp": ("A",),
    "rsqrt": ("A",),
}
#: `op fn` modes: the FIX and MANT bits.
FN_MODE = {"exp": (0, 0), "fix": (1, 0), "mant": (0, 1), "half": (1, 1)}
FIN_DST = {"R0": 0, "R1": 1, "R2": 2, "T": 3}
LOADS = {"table": S.LOAD_TABLE, "cols": S.LOAD_COLS, "rows": S.LOAD_ROWS}
#: The profile a machine names none for (scripts/py/sru/profiles.py strong).
PROFILE = {"F_LANES": 16, "H_TAPS": 2}


class RowReader:
    """Reads one unit's `send` block. `held` is the configuration the unit
    holds from earlier sends of this program: ``{address: value}``."""

    def __init__(self, node, held: dict) -> None:
        self.node = node
        self.fail = node.fail
        self.held = held
        prof = {**PROFILE, **node.p.machine.params.get("SR", {})}
        self.f_lanes, self.h_taps = prof["F_LANES"], prof["H_TAPS"]
        self.job: dict = {}
        self.passes: list = []
        self.ops: list = []
        self.fin: list = []

    def ops_of(self, body) -> list:
        out = []
        for st in body:
            if st.target is not None:
                self.fail(st, f"`{st.op}` binds no name")
            match st.op:
                case "job":
                    self.read_job(st)
                case "pass":
                    self.read_pass(st)
                case "load":
                    out.append(self.load(st))
                case "run":
                    cfg = self.config(st)
                    if cfg.words:
                        out.append(cfg)
                    out.append(self.run(st))
                case "store":
                    out.append(self.store(st))
                case "post":
                    pos, kw = st.positional(), st.kwargs()
                    if pos or set(kw) != {"n", "to"}:
                        self.fail(st, "post n=CREDITS to=UNIT")
                    out.append(
                        E.Post(self.node.unit(st, kw["to"]), self.c(st, kw["n"]))
                    )
                case _:
                    self.fail(
                        st,
                        "a row unit's send holds job pass load run store post, "
                        f"not {st.op!r}",
                    )
            if st.body and st.op != "pass":
                self.fail(st, f"`{st.op}` takes no block")
        return out

    # ------------------------------------------------------------ values
    def c(self, st, t) -> int:
        return self.node.const(st, t)

    def num(self, st, t) -> float:
        """A number: an integer, a float, `inf`, or one negated."""
        if isinstance(t, Neg):
            return -self.num(st, t.x)
        if isinstance(t, Int | Float):
            return float(t.value)
        if t == Name("inf"):
            return float("inf")
        self.fail(st, f"wanted a number, got {t!r}")

    # --------------------------------------------------------------- job
    def read_job(self, st) -> None:
        pos, kw = st.positional(), st.kwargs()
        g = {k: 0 for k in S.G}
        for p in pos:
            if not isinstance(p, Name) or p.text not in FLAGS:
                self.fail(st, f"job flags are {sorted(FLAGS)}, got {p!r}")
            g[JOB[p.text]] = 1
        for k, v in kw.items():
            if k not in JOB:
                self.fail(st, f"no job field {k!r}; they are {sorted(JOB)}")
            if k == "out" and isinstance(v, Name):
                if v.text not in OUT:
                    self.fail(st, f"out= {' | '.join(OUT)}")
                g["OUT"] = OUT[v.text]
            elif k in E8_JOB:
                g[JOB[k]] = S.e8(self.num(st, v))
            else:
                x = self.c(st, v) if not isinstance(v, Neg) else -self.c(st, v.x)
                g[JOB[k]] = x & ((1 << S.G[JOB[k]][2]) - 1)
        self.job = g
        self.passes, self.ops, self.fin = [], [], []

    def read_pass(self, st) -> None:
        if not self.job:
            self.fail(st, "a pass follows its job")
        pos = st.positional()
        if not pos or not isinstance(pos[0], Int):
            self.fail(st, "pass P [r2] [emit] (a block of op and fin)")
        p = pos[0].value
        flags = {x.text for x in pos[1:] if isinstance(x, Name)}
        if len(pos) - 1 != len(flags) or flags - {"r2", "emit"}:
            self.fail(st, "pass P [r2] [emit]")
        if not 0 <= p < S.NPASS or any(q == p for q, _ in self.passes):
            self.fail(st, f"pass {p}: one of 0..{S.NPASS - 1}, once each")
        ops = [s for s in st.body if s.op == "op"]
        fins = [s for s in st.body if s.op == "fin"]
        if len(ops) + len(fins) != len(st.body):
            self.fail(st, "a pass block holds op and fin")
        d = {
            "NOPS": len(ops),
            "OPBASE": len(self.ops) if ops else 0,
            "R2EN": int("r2" in flags),
            "EMIT": int("emit" in flags),
            "FINLEN": len(fins),
            "FINBASE": len(self.fin) if fins else 0,
        }
        self.ops += [self.op(s, len(ops)) for s in ops]
        self.fin += [self.fin_op(s) for s in fins]
        if len(self.ops) > S.OP_DEPTH or len(self.fin) > S.FIN_DEPTH:
            self.fail(
                st, f"the op store holds {S.OP_DEPTH}, the finalise {S.FIN_DEPTH}"
            )
        self.passes.append((p, d))

    def op(self, st, n: int) -> dict:
        pos, kw = st.positional(), st.kwargs()
        kind = pos[0].text if pos and isinstance(pos[0], Name) else None
        flags = {x.text for x in pos[1:] if isinstance(x, Name)}
        if kind == "fma":
            if set(kw) != {"a", "b", "c"} or flags:
                self.fail(st, "op fma a=SRC b=SRC c=SRC")
            f = {"KIND": 0}
            self.src(st, f, "A", kw["a"], n)
            self.src(st, f, "B", kw["b"], n)
            self.src(st, f, "C", kw["c"], n)
            return f
        if kind == "fn":
            modes = flags & set(FN_MODE)
            if (
                not {"a", "k"} <= set(kw) <= {"a", "k", "cs", "pre"}
                or flags - set(FN_MODE) - {"neg"}
                or len(modes) > 1
                or ("neg" in flags and not modes & {"mant", "half"})
            ):
                self.fail(
                    st, "op fn a=SRC k=SRC [fix | mant [neg] | half [neg]] [cs=N] [pre=N]"
                )
            fix, mant = FN_MODE[modes.pop() if modes else "exp"]
            f = {
                "KIND": 1,
                "FIX": fix,
                "MANT": mant,
                "NEG": int("neg" in flags),
                "CS": self.c(st, kw.get("cs", Int(0))),
                "PRE": self.signed(st, kw.get("pre", Int(0)), 6),
            }
            self.src(st, f, "A", kw["a"], n)
            self.src(st, f, "B", kw["k"], n)
            return f
        self.fail(st, "op fma ... | op fn ...")

    def signed(self, st, t, bits: int) -> int:
        v = -self.c(st, t.x) if isinstance(t, Neg) else self.c(st, t)
        if not -(1 << (bits - 1)) <= v < 1 << (bits - 1):
            self.fail(st, f"{v} does not fit {bits} signed bits")
        return v & ((1 << bits) - 1)

    def src(self, st, f: dict, slot: str, t, n: int) -> None:
        """Operand `slot` (A, B, C) of an op written `t`: X B O (A), a
        number, R0..R2, C0 C1, B, X (B, C), or H<k> (op k's result); A and C
        may be negated. The names are the hardware's, upper case, so no
        parameter (lower case) binds over them."""
        neg = isinstance(t, Neg) and not isinstance(t.x, Int | Float)
        if neg:
            if slot == "B":
                self.fail(st, "b is not negated: negate a or c")
            f["N" + slot] = 1
            t = t.x
        name = t.text if isinstance(t, Name) else None
        if name and name[0] == "H" and name[1:].isdigit():
            k = int(name[1:])
            if k >= n:
                self.fail(st, f"{name}: the program has {n} ops")
            f[slot] = ("H", k)
        elif slot == "A":
            if name not in ("X", "B", "O"):
                self.fail(st, f"a is X, B, O or H<k>, got {t!r}")
            f["A"] = name
        elif name in ("X", "B"):
            f[slot] = name
        elif name in ("R0", "R1", "R2"):
            f[slot], f["R" + slot] = "R", int(name[1])
        elif name in ("C0", "C1"):
            f[slot], f["C" + slot] = "C", int(name[1])
        else:
            f[slot], f["K" + slot] = "K", S.e8(self.num(st, t))

    def fin_op(self, st) -> dict:
        pos = st.positional()
        form = "fin OP DST <- SRC[, SRC...]"
        if len(pos) < 4 or pos[2] != Arrow("<-") or not isinstance(pos[0], Name):
            self.fail(st, form)
        op = pos[0].text
        if op not in FIN_ARGS:
            self.fail(st, f"finalise ops are {sorted(FIN_ARGS)}")
        if not isinstance(pos[1], Name) or pos[1].text not in FIN_DST:
            self.fail(st, f"a finalise writes {' '.join(FIN_DST)}")
        args = pos[3:]
        if len(args) != len(FIN_ARGS[op]):
            self.fail(st, f"fin {op} takes {len(FIN_ARGS[op])} sources")
        f = {"OP": S.FOP[op], "DST": FIN_DST[pos[1].text], "A": 0, "B": 0, "C": 0}
        for slot, a in zip(FIN_ARGS[op], args, strict=True):
            key = a.text if isinstance(a, Name) else None
            if key not in S.SRC:
                self.fail(st, f"finalise sources are {' '.join(S.SRC)}")
            f[slot] = S.SRC[key]
        return f

    # ------------------------------------------------------- instructions
    def words(self, st) -> dict:
        if not self.job:
            self.fail(st, "a run follows its job")
        passes = [{k: 0 for k in S.P} for _ in range(S.NPASS)]
        for p, d in self.passes:
            passes[p] = d
        g = dict(self.job)
        top = max((p for p, _ in self.passes), default=0)
        if not g["BUF"] and top:
            self.fail(st, "passes past 0 replay the buffer: job buf")
        if g["BUF"] and not g["NPASS"]:
            g["NPASS"] = top
        try:
            return S.words(g, passes, self.ops, self.fin, self.f_lanes, self.h_taps)
        except ValueError as e:
            self.fail(st, f"the pool cannot run this program: {e}")

    def config(self, st) -> E.Config:
        want = self.words(st)
        diff = tuple((a, v) for a, v in sorted(want.items()) if self.held.get(a) != v)
        self.held.update(want)
        return E.Config(diff)

    def out_words(self) -> int:
        """Out words a sub-row: f16 one per word, MXFP7 one per two."""
        nw, out = self.job["NW"], self.job["OUT"]
        return nw if out <= 1 else nw // 2

    def run(self, st) -> E.Run:
        kw = st.kwargs()
        flags = {x.text for x in st.positional() if isinstance(x, Name)}
        if (
            st.positional() != [Name(f) for f in sorted(flags)]
            or flags - {"count"}
            or "a" not in kw
            or "out" not in kw
        ):
            self.fail(
                st,
                "run a=ADDR|recv [from=UNIT] [astride=W] [b=ADDR] [bstride=W] "
                "out=ADDR|UNIT [ostride=W] [buf=B] [at=GRANULE] [count]",
            )
        nw = self.job["NW"]
        a = None if kw["a"] == Name("recv") else self.c(st, kw["a"])
        if a is None and "from" not in kw:
            self.fail(st, "a=recv names its producer: from=UNIT")
        to = self.node.unit(st, kw["out"]) if isinstance(kw["out"], View) else None
        return E.Run(
            a,
            self.c(st, kw.get("astride", Int(nw))),
            out=0 if to else self.c(st, kw["out"]),
            ostride=self.c(st, kw.get("ostride", Int(self.out_words()))),
            b=self.c(st, kw.get("b", Int(0))),
            bstride=self.c(st, kw.get("bstride", Int(nw))),
            src=self.node.unit(st, kw["from"]) if "from" in kw else (0, 0),
            to=to,
            buf=self.c(st, kw.get("buf", Int(0))),
            at=self.c(st, kw.get("at", Int(0))),
            count="count" in flags,
            words=nw * self.job["NSR"],
        )

    def load(self, st) -> E.Load:
        pos, kw = st.positional(), st.kwargs()
        if (
            len(pos) != 3
            or not isinstance(pos[0], Name)
            or pos[0].text not in LOADS
            or pos[1] != Arrow("<-")
            or "n" not in kw
        ):
            self.fail(st, "load table|cols|rows <- ADDR n=WORDS [at=FIRST]")
        return E.Load(
            LOADS[pos[0].text],
            self.c(st, pos[2]),
            self.c(st, kw["n"]),
            self.c(st, kw.get("at", Int(0))),
        )

    def store(self, st) -> E.Store:
        pos, kw = st.positional(), st.kwargs()
        if (
            len(pos) != 3
            or pos[0] != Name("rows")
            or pos[1] != Arrow("->")
            or "n" not in kw
        ):
            self.fail(st, "store rows -> ADDR n=ROWS [at=FIRST]")
        return E.Store(
            self.c(st, pos[2]), self.c(st, kw["n"]), self.c(st, kw.get("at", Int(0)))
        )


__all__ = ["RowReader"]
