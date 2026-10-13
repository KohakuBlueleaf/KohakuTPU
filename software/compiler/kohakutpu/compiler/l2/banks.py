"""Bank-aware placement of a vector core's values.

A V2 register file is two banks (a word's chunk bit 0), each RF_COPIES read
copies. With two copies, a math beat reading three distinct words of one
bank holds the math engine a cycle (`v2_core` `m_hold`), and a beat reading
two words of one bank leaves the pack engine no copy of that bank for the
cycle (math reads first, `pk_ok`); with three copies only the second holds,
at three words. A value stored rotated by one chunk -- logical chunk i in
register chunk i + 1 -- reads the other bank on every beat. `rotate` picks a
rotation (0 or 1) per value so the code's beats read a bank fewer times than
it has copies wherever a choice exists, and rewrites every reference: a
chunk select moves by the rotation (mod 8; the core's chunk counters wrap),
a bare operand takes one, an unpack or pack names its chunk count.

A value read or written whole by a mode that takes no chunk select (an MX7
pack, a GT4 walk) keeps rotation 0.
"""

import re

from kohakutpu.compiler.verify.model import Finding
from kohakutpu.language.l1.ops import MATH
from kohakutpu.language.text.meta import Expander
from kohakutpu.language.text.syntax import Int, emit

CHUNKS = 8
#: the cost of a held math beat and of a beat that leaves the pack no copy
HOLD = 2
STARVE = 1
#: local-search passes over the values
PASSES = 16

_REF = re.compile(r"\{(\d+)\}(?:\[(\d+)\s*(:?)\s*\])?")
_REG = re.compile(r"\bv(\d+)\b")


def _operands(rest: str) -> list:
    out, depth, cur = [], 0, ""
    for ch in rest:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def _walk_op(tmpl: str) -> bool:
    return tmpl.startswith(("vunpk ", "vpack "))


def _fixed(tmpl: str) -> bool:
    """An op whose register operand takes no chunk select."""
    return _walk_op(tmpl) and (" mx7" in tmpl or " gt4" in tmpl)


def _reads(tmpl: str, syms: tuple) -> list:
    """``[(symbol, base, stride)]`` a math op reads a beat, or [] for a walk."""
    if _walk_op(tmpl):
        return []
    _, _, rest = tmpl.partition(" ")
    body = re.sub(r"\s+(vl|if|unless)=\S+", "", rest)
    out = []
    for opnd in _operands(body)[1:]:
        m = _REF.search(opnd)
        if m is None:
            continue
        base = int(m[2]) if m[2] is not None else 0
        stride = 0 if m[2] is not None and not m[3] else 1
        out.append((syms[int(m[1])], base, stride))
    return out


def _beats(tmpl: str) -> int:
    vl = re.search(r"\bvl=(\d+)", tmpl)
    return max(1, -(-int(vl[1]) // 16)) if vl else CHUNKS


def _cost(reads: list, beats: int, rho: dict, copies: int) -> int:
    cost = 0
    for i in range(beats):
        words = {(s, (b + rho.get(s, 0) + st * i) % CHUNKS) for s, b, st in reads}
        for bank in (0, 1):
            n = sum(1 for _, c in words if c % 2 == bank)
            if copies == 2:
                cost += HOLD * (n >= 3) + STARVE * (n >= 2)
            elif copies == 3:
                cost += STARVE * (n >= 3)
    return cost


def choose(code: list, copies: int) -> dict:
    """symbol -> rotation for the ``(template, syms)`` entries of `code`."""
    if copies >= 4:
        return {}
    ops = [
        (_reads(t, syms), _beats(t))
        for t, syms in (c for c in code if isinstance(c, tuple))
        if not _walk_op(t)
    ]
    fixed = {
        s
        for t, syms in (c for c in code if isinstance(c, tuple))
        if _fixed(t)
        for s in syms
    }
    touching: dict = {}
    for k, (reads, _) in enumerate(ops):
        for s, _, _ in reads:
            touching.setdefault(s, set()).add(k)
    free = [s for s in touching if s not in fixed]
    rho: dict = {}
    for _ in range(PASSES):
        moved = False
        for s in free:
            ks = touching[s]
            now = sum(_cost(*ops[k], rho, copies) for k in ks)
            rho[s] = 1 - rho.get(s, 0)
            if sum(_cost(*ops[k], rho, copies) for k in ks) < now:
                moved = True
            else:
                rho[s] = 1 - rho[s]
        if not moved:
            break
    return {s: r for s, r in rho.items() if r}


def apply(tmpl: str, syms: tuple, rho: dict) -> str:
    """`tmpl` with each reference to a rotated symbol moved by its rotation."""
    if not any(rho.get(s) for s in syms):
        return tmpl
    walk = _walk_op(tmpl)
    cb = 0

    def move(m) -> str:
        nonlocal cb
        r = rho.get(syms[int(m[1])], 0)
        if not r:
            return m[0]
        if m[2] is None:
            return f"{{{m[1]}}}[{r}:]"
        cb = int(m[2])
        return f"{{{m[1]}}}[{(int(m[2]) + r) % CHUNKS}{m[3]}]"

    out = _REF.sub(move, tmpl)
    if walk and " n=" not in out:
        out += f" n={CHUNKS - cb}"
    return out


def rotate(code: list, copies: int) -> list:
    """`code` with its values placed by `choose`."""
    rho = choose(code, copies)
    if not rho:
        return code
    return [
        c if not isinstance(c, tuple) else (apply(c[0], c[1], rho), c[1]) for c in code
    ]


def symbolic(text: str) -> tuple:
    """An image line as ``(template, registers)``."""
    regs: list = []
    for r in _REG.findall(text):
        if f"v{r}" not in regs:
            regs.append(f"v{r}")
    escaped = text.replace("{", "{{").replace("}", "}}")
    return _REG.sub(lambda m: "{" + str(regs.index(f"v{m[1]}")) + "}", escaped), tuple(
        regs
    )


def lint(module, copies: int) -> list:
    """Every math statement of `module`'s images whose beats read a bank more
    times than a `copies`-copy register file serves: ``Finding("bank", image,
    text)``, the beats that hold the math engine (three words of a bank on
    two copies) and those that leave the pack engine no copy."""
    out = []
    r = module.reader()
    macros = {m.name: (m.params, m.stmts) for m in module.macros.values()}
    for img in module.images.values():
        env = dict.fromkeys(img.params, Int(0))
        todo = list(Expander(macros, r.fail, unroll_all=True).stmts(img.stmts, env))
        while todo:
            st = todo.pop(0)
            if st.body:
                todo[:0] = st.body
                continue
            if st.op not in MATH:
                continue
            text = emit([st], 0).strip()
            tmpl, regs = symbolic(text)
            reads = _reads(tmpl, regs)
            held = starved = 0
            for i in range(_beats(tmpl)):
                words = {(s, (b + st_ * i) % CHUNKS) for s, b, st_ in reads}
                for bank in (0, 1):
                    n = sum(1 for _, c in words if c % 2 == bank)
                    held += copies == 2 and n >= 3
                    starved += n >= copies
            if held or starved:
                what = []
                if held:
                    what.append(f"{held} beats hold the math engine")
                if starved:
                    what.append(f"{starved} leave the pack engine no copy")
                out.append(
                    Finding(
                        "bank",
                        f"image {img.name}",
                        f"line {st.line}: {text}: {', '.join(what)}",
                    )
                )
    return out


__all__ = ["apply", "choose", "lint", "rotate", "symbolic"]
