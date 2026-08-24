#!/usr/bin/env python3
"""Verilog readability rules, checked and optionally fixed.

    python scripts/py/vstyle.py                 # report
    python scripts/py/vstyle.py --fix-decls     # apply F3 only
    python scripts/py/vstyle.py --show src/kohakuaccel/sysnode/core/mag.v
    python scripts/py/vstyle.py --lines src/kohakuaccel/pe/rv32/rv_pe.v
    python scripts/py/vstyle.py src/kohakuaccel/sysnode

`format-example2.v` is the normative statement of the style and the numbering
here is its: this script checks the six rules of F1-F12 that are mechanically
checkable. Verible would be the tool for this, but it is not packaged for win-64
and it would not decide any of these anyway -- a formatter reflows whitespace,
and F4/F5/F7 are structure while F6 is which token starts a line.

F3  a declaration is ONE line. Several names on that line are fine -- the
    example keeps `localparam [3:0] A_ADD = 4'd0, A_SUB = 4'd1, ...;` -- what it
    never does is run one over a continuation. A hand-aligned block across three
    lines reads as a table and diffs as a paragraph: adding a name in the middle
    re-aligns every line, so the diff touches all of them and says nothing.
F4  begin/end on every if/else/for body, on the next line OR THE SAME ONE.
    Verilog's dangling-else is legal and silent, and a one-line body is one edit
    away from being two statements of which only the first is guarded. F4a is
    the same rule applied to an `always` body.
F5  case items are indented one level inside `case`. The one place begin/end is
    optional is a case item that is a single simple statement.
F6  a multi-line expression is wrapped in its own `(` `)` and its continuation
    lines LEAD with the operator. A trailing `&&` or `?` hides the shape of the
    expression at the right margin, where nothing else is. What this script can
    see is the trailing operator; whether a given expression SHOULD have been
    broken at all is F6's judgement half and is not checkable.
F7  a named generate block. An unnamed one gets a tool-assigned label, so a
    hierarchical reference into it is unstable across tool versions -- which is
    exactly what a bench probe is.

F3 is the only rule this script rewrites: it re-packs one declaration's names
onto full lines, no reordering and no reindentation of anything else. The rest
change control flow, naming or line breaks and are reported for a human.

Every check reports LINE NUMBERS and the count is their length, so `--lines`
and the totals cannot disagree. That is not a convenience: the residue of F4
and F6 is fixed by hand, one file at a time behind that file's bench, and a
count with no location makes that a search rather than an edit.
"""

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]

DEFAULT_DIRS = ("src/kohakuaccel", "src/kohakutpu", "src/kohakumpe")

WIDTH = 80

# `localparam [3:0] A = 4'd0, B = 4'd1;` -- the range is optional, the type may
# be signed, and the statement may run over continuation lines.
DECL = re.compile(
    r"^(?P<indent>[ \t]*)(?P<kw>localparam|parameter)\b(?P<rest>.*?);",
    re.DOTALL | re.MULTILINE,
)

# F4, the same-line form: `if (go) lsu_done <= 1'b0;`. The header's parentheses
# nest, so they are matched by hand rather than by this regex.
SAME_LINE_KW = re.compile(r"(?:^|\s)(if|for)\s*\(")
ELSE_SAME = re.compile(r"\belse\s+(?!if\b|begin\b)\S")

# F4a: `always @(posedge clk) q <= d;` -- a body that is not `begin` and not
# another `if`, which F4 already counts.
ALWAYS = re.compile(r"^[ \t]*always\b")

GEN_REGION = re.compile(r"\bgenerate\b(.*?)\bendgenerate\b", re.DOTALL)
GEN_BLOCK = re.compile(r"\bbegin\b(?!\s*:)")

# F6: a line whose last token is a binary or ternary operator, so the expression
# continues and the reader has to find that out at column 79.
TRAIL_OP = re.compile(
    r"(?:&&|\|\||[?:]|[-+*/%^]|[&|~]|==|!=|<=|>=|<<|>>|<|>)$",
)

CASE_OPEN = re.compile(r"^(?P<indent>[ \t]*)(?:case|casex|casez)\s*\(")
ENDCASE = re.compile(r"^(?P<indent>[ \t]*)endcase\b")


def strip_comment(line):
    """The code half of a line. Crude on purpose: `//` inside a string literal
    does not occur in this tree's RTL, and a block comment is handled by the
    caller stripping those first."""
    return line.split("//")[0].rstrip()


def strip_block_comments(text):
    """Block comments removed, LINE NUMBERING PRESERVED.

    Deleting a multi-line `/* .. */` outright joins the code before it to the
    code after it, so every line below shifts up and a reported line number
    names the wrong line. Replacing it with its own newlines keeps the indexing
    exact at no cost to what the checks see.
    """
    return re.sub(
        r"/\*.*?\*/",
        lambda m: "\n" * m.group(0).count("\n"),
        text,
        flags=re.DOTALL,
    )


def _tail(sep, cmt):
    """A line's trailing comment, re-attached with a space in front of it.

    `f"... begin{sep}{cmt}"` produced `begin// mesh`: legal, and unreadable.
    """
    return f"  {sep}{cmt}" if sep else ""


def _rel(f):
    """`f` relative to the repo, or its own path when it is outside one."""
    try:
        return f.relative_to(ROOT).as_posix()
    except ValueError:
        return f.as_posix()


def _blank_comments(text):
    """Line comments removed, offsets and line numbering preserved."""
    return "\n".join(
        ln[: ln.index("//")] + " " * (len(ln) - ln.index("//")) if "//" in ln else ln
        for ln in text.split("\n")
    )


def close_paren(s, start):
    """Index just past the `(` at `start`'s matching `)`, or None."""
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "(":
            depth += 1
        elif s[i] == ")":
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def same_line_bodies(text):
    """F4, the same-line form: `if (...) stmt;`, `for (...) stmt;` and
    `else stmt;` where stmt is not `begin`. Returns 1-based line numbers, one
    per finding, so a line with two of them appears twice."""
    hits = []
    for i, raw in enumerate(strip_block_comments(text).splitlines(), 1):
        line = strip_comment(raw)
        if not line.strip():
            continue
        for m in SAME_LINE_KW.finditer(line):
            end = close_paren(line, m.end() - 1)
            if end is None:
                continue
            tail = line[end:].strip()
            if tail and not tail.startswith("begin"):
                hits.append(i)
        if ELSE_SAME.search(line):
            hits.append(i)
    return hits


def bare_always(text):
    """F4a: an always whose body is neither `begin` nor an if/for that F4 counts,
    on the header line or the next. Returns 1-based line numbers."""
    hits = []
    lines = strip_block_comments(text).splitlines()
    for i, raw in enumerate(lines):
        line = strip_comment(raw)
        if not ALWAYS.match(line):
            continue
        # `always @* begin` opens its block on the header line. The test below
        # locates the sensitivity list by its `)`, which `@*` does not have, so
        # without this it reads the NEXT line as the body and reports a
        # correctly-formed block.
        if line.rstrip().endswith("begin"):
            continue
        # `always @(posedge clk)` alone on its line: the body is the next one.
        after_sens = line[line.find(")") :] if ")" in line else ""
        header_only = re.fullmatch(r"[ \t]*always\b[^;]*", line) and not re.search(
            r"\)\s*\S", after_sens
        )
        body = (
            strip_comment(lines[i + 1]) if header_only and i + 1 < len(lines) else line
        )
        tail = body.strip()
        # Everything after the last `)` of the sensitivity list, if there is one.
        if ")" in tail:
            tail = tail[tail.rfind(")") + 1 :].strip()
        if not tail or tail.startswith(("begin", "if", "for")):
            continue
        hits.append(i + 1)
    return hits


def next_line_bodies(text):
    """F4, the next-line form: a complete header whose body is on the line below
    and does not open a block.

    The header is matched by balancing its parenthesis, not by a regex ending in
    `)`. `if ((SEED_UNITS != 0)` is a CONDITION broken across lines and its next
    line is the rest of the condition, not a body -- eleven of those were being
    reported as missing begin/end.

    Returns 1-based line numbers, naming the HEADER rather than the body: the
    header is the line that gets ` begin` appended.
    """
    hits = []
    lines = strip_block_comments(text).splitlines()
    for i, raw in enumerate(lines):
        code = strip_comment(raw)
        # `always` is F4a's, not F4's, or the two double-count it.
        if ALWAYS.match(code):
            continue
        if not HDR_ALONE.match(code) or not _header_only(code):
            continue
        j = _next_code(lines, i + 1)
        if j is None:
            continue
        body = strip_comment(lines[j]).strip()
        if body.startswith(("begin", "`")):
            continue
        hits.append(i + 1)
    return hits


def trailing_operators(text):
    """F6: continuation lines announced by a trailing operator. Returns 1-based
    line numbers."""
    hits = []
    for i, raw in enumerate(strip_block_comments(text).splitlines(), 1):
        line = strip_comment(raw)
        s = line.strip()
        if not s or s.startswith("`"):
            continue
        # A `?` or `:` ending a case item label is not a continuation.
        if re.match(r"^[\w'\{\}\[\]:,\s]+:$", s):
            continue
        if TRAIL_OP.search(s):
            hits.append(i)
    return hits


def flat_case_items(text):
    """F5: a case whose first item is not indented past the `case` keyword.
    Returns 1-based line numbers, naming the `case` line."""
    hits = []
    lines = strip_block_comments(text).splitlines()
    for i, raw in enumerate(lines):
        m = CASE_OPEN.match(strip_comment(raw))
        if not m:
            continue
        base = len(m.group("indent").expandtabs(4))
        for nxt in lines[i + 1 :]:
            code = strip_comment(nxt)
            if not code.strip():
                continue
            ind = len(code) - len(code.lstrip())
            if ENDCASE.match(code):
                break
            if ind <= base:
                hits.append(i + 1)
            break
    return hits


GEN_TOK = re.compile(
    r"\b(generate|endgenerate|always|initial|function|endfunction|task|endtask"
    r"|begin|end)\b(?:\s*:\s*(\w+))?"
)


def unnamed_generate_blocks(text):
    """F7: an unnamed ELABORATION-TIME `begin` inside a generate region.

    A `begin` inside an always block needs no label even when that always block
    sits in a generate region -- and counting those is how this measured 385,
    then 188, when the real figure is a fraction of either. `noc_l2_adapter.v`
    wraps its whole body in one `generate if (PASS)`, so a naive count reports
    every procedural block in the file.

    So the walk tracks whether it is inside procedural code: `always`, `initial`,
    `function` and `task` open it, and only a `begin` outside it is a generate
    block that a hierarchical reference can name.

    Returns 1-based line numbers of the offending `begin`.

    LINE COMMENTS ARE STRIPPED FIRST. This walk is the only check that reads
    words rather than line shapes, so a `//` comment saying the word `begin`
    inside a generate region counted as an unnamed block -- which is what both
    normative example files reported, being the two that talk about the rule.
    """
    hits = []
    src = _blank_comments(strip_block_comments(text))
    for m in GEN_REGION.finditer(src):
        # One frame per open block. `armed` means an always/initial/function/task
        # has been seen at THIS level, so any begin here belongs to it. It is not
        # cleared by the first begin: an always whose body is
        # `if (..) begin .. end else begin .. end` opens two blocks at the same
        # level, and clearing after the first makes the `else` look like a
        # generate block. Erring toward procedural undercounts; the reverse
        # reported 188 when the answer is a dozen.
        stack = [{"proc": False, "armed": False}]
        base = m.start(1)
        for t in GEN_TOK.finditer(m.group(1)):
            tok, label = t.group(1), t.group(2)
            top = stack[-1]
            if tok in ("always", "initial", "function", "task"):
                top["armed"] = True
            elif tok == "begin":
                proc = top["armed"] or top["proc"]
                if not proc and not label:
                    hits.append(src.count("\n", 0, base + t.start()) + 1)
                stack.append({"proc": proc, "armed": False})
            elif tok == "end":
                if len(stack) > 1:
                    stack.pop()
            elif tok in ("endfunction", "endtask"):
                top["armed"] = False
    return sorted(hits)


def split_top(s):
    """Split on commas that are not inside brackets, braces or parens."""
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


def decl_type_and_names(rest):
    """(type_prefix, [assignments]) for a declaration body, or None if unsafe.

    The type prefix is everything before the first `=`'s name: a width, a
    signedness, or nothing. Returns None when the shape is not a plain list of
    `NAME = value` -- a typedef, an unpacked dimension or anything else keeps
    its original text.
    """
    parts = split_top(rest)
    first = parts[0]
    if "=" not in first:
        return None
    head, _, firstval = first.partition("=")
    m = re.match(r"^(?P<pre>.*?)(?P<name>[A-Za-z_]\w*)\s*$", head, re.DOTALL)
    if not m:
        return None
    pre = " ".join(m.group("pre").split())
    items = [(m.group("name"), " ".join(firstval.split()))]
    for p in parts[1:]:
        if "=" not in p:
            return None
        n, _, v = p.partition("=")
        n = n.strip()
        if not re.fullmatch(r"[A-Za-z_]\w*", n):
            return None
        items.append((n, " ".join(v.split())))
    return pre, items


def pack(lead, items):
    """The names re-packed onto full lines, greedily to WIDTH. One name that
    does not fit still gets its own line rather than a continuation."""
    lines, cur = [], []
    for name, val in items:
        trial = cur + [f"{name} = {val}"]
        if cur and len(lead) + len(", ".join(trial)) + 1 > WIDTH:
            lines.append(f"{lead}{', '.join(cur)};")
            cur = [f"{name} = {val}"]
        else:
            cur = trial
    if cur:
        lines.append(f"{lead}{', '.join(cur)};")
    return lines


BODY_OPENS = ("begin", "if", "for", "while", "case", "casez", "casex", "fork")


def _one_statement(s):
    """True when `s` is exactly one complete statement safe to wrap.

    DELIBERATELY NARROW. A wrong wrap changes behaviour silently, so anything
    that is not a single bracket-balanced statement ending in `;` -- and is not
    itself a block opener -- is left for a human.
    """
    s = s.strip()
    if not s.endswith(";") or s.count(";") != 1:
        return False
    # A continuation of the line above, not a statement. `_header_only` should
    # already have refused the header, so this is the second line of defence.
    if s[:1] in ("?", ":"):
        return False
    # A case ITEM: `8'h58: c_src <= ...;` cleared every test below, because a
    # sized literal does not start with a letter.
    if re.match(
        r"^(?:default|[0-9]+'[bodhBODH][0-9a-fA-FxzXZ_]+|[A-Z_]\w*)"
        r"(?:\s*,\s*\S+)*\s*:(?!:)",
        s,
    ):
        return False
    # A block opener is never a body this may wrap: `if (a) if (b) x;` has two
    # bodies and wrapping the outer one changes which `else` binds where.
    first = re.match(r"[A-Za-z_]\w*", s)
    if first and first.group(0) in BODY_OPENS:
        return False
    depth = {"(": 0, "[": 0, "{": 0}
    pairs = {")": "(", "]": "[", "}": "{"}
    for ch in s:
        if ch in depth:
            depth[ch] += 1
        elif ch in pairs:
            depth[pairs[ch]] -= 1
    return all(v == 0 for v in depth.values())


HDR = re.compile(
    r"^(?P<ind>[ \t]*)(?P<hdr>(?:end\s+)?(?:else\s+)?"
    r"(?:if|for|while)\s*\(|always\b|else\b)"
)

# A header alone on its line, body on the next. `else` alone is excluded: house
# style writes `end else begin`, so wrapping it needs a merge with the line
# above and that is a human's call.
HDR_ALONE = re.compile(
    r"^(?P<ind>[ \t]*)(?:(?:end\s+)?(?:else\s+)?(?:if|for|while)\s*\(|always\b)"
)


def _next_code(lines, i):
    """Index of the next line with code on it, or None."""
    for j in range(i, len(lines)):
        if strip_comment(lines[j]).strip():
            return j
    return None


_OPENS = re.compile(r"\b(begin|case|casex|casez)\b")
_CLOSES = re.compile(r"\b(end|endcase)\b")


def _block_span(lines, j):
    """Last line index of the complete construct starting at `lines[j]`, or None.

    `for (..) STMT` and `for (..) begin STMT end` are the same statement for any
    single STMT, so wrapping a body that is itself a complete if/for/case is
    behaviour-preserving by construction. The only hazard is a dangling `else`,
    which is why the span is extended through one when it follows.
    """
    first = re.match(r"[A-Za-z_]\w*", strip_comment(lines[j]).strip())
    if not first or first.group(0) not in (
        "if",
        "for",
        "while",
        "case",
        "casex",
        "casez",
    ):
        return None
    # ONLY an `if` body may absorb a following `else`. A case/for/while cannot
    # take one, so an `else` after such a body belongs to the header being
    # wrapped, and pulling it inside the new block orphans it: two sites in
    # khs_unit.v broke exactly that way, and a syntax error is the only reason
    # it showed rather than binding somewhere else silently.
    kind = first.group(0)
    depth = 0
    for k in range(j, len(lines)):
        code = strip_comment(lines[k])
        s = code.strip()
        if not s:
            continue
        depth += len(_OPENS.findall(code)) - len(_CLOSES.findall(code))
        if depth < 0:
            return None
        # `end else begin` nets to zero and is NOT a close, which is why the
        # trailing token is tested rather than the depth alone.
        if depth > 0 or s.endswith(("begin", "else")):
            continue
        if not s.endswith(";") and not _CLOSES.search(code):
            continue
        nxt = _next_code(lines, k + 1)
        if (
            kind == "if"
            and nxt is not None
            and strip_comment(lines[nxt]).strip().startswith("else")
        ):
            continue
        return k
    return None


def _header_only(code):
    """True when `code` is EXACTLY a block header, nothing after its `)`.

    Matching a leading `if (`/`for (`/`always` and a trailing `)` is not the
    same test, and the difference corrupted three source files: `if (rd_take)
    rr_rd <= (rd_sel == N - 1)` both starts with a header and ends with `)`, as
    does `if (cfg_en) case (cfg_addr)` and any `if (c) x <= a ? f(b)` broken
    across lines. Each was read as a bare header, so the line BELOW it -- a
    ternary continuation or a case item -- was wrapped in begin/end, which is a
    syntax error rather than a silent behaviour change. That is the only reason
    it was caught at all, so the paren is matched properly here.
    """
    m = HDR_ALONE.match(code)
    if not m:
        return False
    op = code.find("(", m.start())
    if op < 0:
        return False
    close = close_paren(code, op)
    return close is not None and not code[close:].strip()


def rewrite_blocks_next(text):
    """F4: begin/end around a single-statement body on the NEXT line.

    Refused when an `else` follows the body, because closing the block then has
    to become `end else` on the line above and a wrong merge moves a statement
    between branches. Refused for a bare `else` header for the same reason.
    """
    lines = text.splitlines(True)
    out, n, skip = [], 0, set()
    for i, raw in enumerate(lines):
        if i in skip:
            continue
        code = strip_comment(raw)
        m = HDR_ALONE.match(code)
        if not m or not _header_only(code):
            out.append(raw)
            continue
        j = _next_code(lines, i + 1)
        if j is None:
            out.append(raw)
            continue
        # The body may SPAN LINES -- a wrapped `$display` is one statement over
        # three -- so the span is joined for the balance test and then re-emitted
        # verbatim, indentation and all. Joining only to TEST keeps the rewrite a
        # pure insertion of `begin` and `end`.
        # A body that is itself a complete if/for/case is one statement too.
        blk = _block_span(lines, j)
        if blk is not None:
            end = blk
            k = _next_code(lines, end + 1)
            if k is not None and strip_comment(lines[k]).strip().startswith("else"):
                out.append(raw)
                continue
            ind = m.group("ind")
            nl = "\n" if raw.endswith("\n") else ""
            out.append(f"{code.rstrip()} begin{nl}")
            for t in range(i + 1, j):
                out.append(lines[t])
            body_ind = len(lines[j]) - len(lines[j].lstrip())
            shift = len(ind) + 4 - body_ind
            for t in range(j, end + 1):
                s = lines[t]
                if shift >= 0:
                    out.append(" " * shift + s)
                else:
                    cur = len(s) - len(s.lstrip())
                    out.append(s[min(-shift, cur) :])
            out.append(f"{ind}end{nl}")
            skip.update(range(i + 1, end + 1))
            n += 1
            continue

        end = j
        joined = strip_comment(lines[j]).strip()
        while not _one_statement(joined) and end + 1 < len(lines):
            nxt = _next_code(lines, end + 1)
            if nxt is None or nxt - end > 1:
                break
            end = nxt
            joined = joined + " " + strip_comment(lines[end]).strip()
            if joined.count(";") > 1:
                break
        if not _one_statement(joined):
            out.append(raw)
            continue
        # An `else` may follow: `end` goes on its own line, never merged into
        # `end else`. Safe only here -- `_one_statement` has already rejected a
        # body that opens a block, so the `else` cannot be the body's.
        ind = m.group("ind")
        nl = "\n" if raw.endswith("\n") else ""
        out.append(f"{code.rstrip()} begin{nl}")
        for t in range(i + 1, j):
            out.append(lines[t])
        # Shift the whole span so its FIRST line lands at ind + 4, keeping the
        # continuation lines' relative alignment -- a wrapped argument list stays
        # lined up under its opening parenthesis.
        body_ind = len(lines[j]) - len(lines[j].lstrip())
        shift = len(ind) + 4 - body_ind
        for t in range(j, end + 1):
            s = lines[t]
            if shift >= 0:
                out.append(" " * shift + s)
            else:
                cur = len(s) - len(s.lstrip())
                out.append(s[min(-shift, cur) :])
        out.append(f"{ind}end{nl}")
        j = end
        # Every line consumed, not just the body: the comment lines between the
        # header and the body were emitted inside the block above and would
        # otherwise be emitted a second time after the `end`.
        skip.update(range(i + 1, j + 1))
        n += 1
    return "".join(out), n


def rewrite_blocks(text):
    """F4/F4a: begin/end around a single-statement body on the SAME line.

    Only the same-line form, and only when the body passes `_one_statement`.
    The next-line form and anything with an `else` on the line are reported,
    never rewritten: those change which branch a statement belongs to if the
    match is wrong, and that is not a risk worth a tool.
    """
    out, n = [], 0
    for raw in text.splitlines(True):
        nl = "\n" if raw.endswith("\n") else ""
        line = raw[: len(raw) - len(nl)]
        code, sep, cmt = line.partition("//")
        m = HDR.match(code)
        if not m or "else" in code[m.end() :] or not code.rstrip().endswith(";"):
            out.append(raw)
            continue
        head = code[: m.end()]
        rest = code[m.end() :]
        if head.rstrip().endswith("("):
            close = close_paren(code, m.end() - 1)
            if close is None:
                out.append(raw)
                continue
            head, rest = code[:close], code[close:]
        elif head.rstrip().endswith("always"):
            at = code.find("@")
            if at >= 0:
                close = close_paren(code, code.find("(", at))
                if close is None:
                    out.append(raw)
                    continue
                head, rest = code[:close], code[close:]
        if not _one_statement(rest):
            out.append(raw)
            continue
        ind = m.group("ind")
        out.append(f"{head.rstrip()} begin{_tail(sep, cmt)}{nl}")
        out.append(f"{ind}    {rest.strip()}{nl}")
        out.append(f"{ind}end{nl}")
        n += 1
    return "".join(out), n


ELSE_TAIL = re.compile(
    r"^(?P<ind>[ \t]*)(?P<lead>(?:end[ \t]+)?)else[ \t]+(?P<st>\S.*)$"
)
ITEM_IF = re.compile(
    r"^(?P<ind>[ \t]*)(?P<label>(?:default|[^\s:?]+)[ \t]*:)[ \t]*"
    r"(?P<kw>if|for)[ \t]*\("
)


def rewrite_tails(text):
    """F4: two same-line shapes `rewrite_blocks` leaves alone.

    `else X;` is `else begin X; end` for any single statement, and a case item's
    `LABEL: if (c) X;` needs begin/end on the `if` only -- F5 already allows a
    case item whose body is one statement, and an if/begin/end IS one.

    `else` starting a statement that RUNS ON -- a wrapped `$display` is the
    common one -- is joined only to test completeness and re-emitted verbatim.
    """
    lines = text.splitlines(True)
    out, n, skip = [], 0, set()
    for i, raw in enumerate(lines):
        if i in skip:
            continue
        nl = "\n" if raw.endswith("\n") else ""
        line = raw[: len(raw) - len(nl)]
        code, sep, cmt = line.partition("//")

        m = ITEM_IF.match(code)
        if m:
            close = close_paren(code, code.index("(", m.end("kw")))
            if close is not None:
                head, rest = code[:close], code[close:]
                if _one_statement(rest):
                    ind = m.group("ind")
                    out.append(f"{head.rstrip()} begin{_tail(sep, cmt)}{nl}")
                    out.append(f"{ind}    {rest.strip()}{nl}")
                    out.append(f"{ind}end{nl}")
                    n += 1
                    continue

        m = ELSE_TAIL.match(code)
        if m and not m.group("st").startswith(("if", "begin")):
            st = m.group("st").strip()
            ind = m.group("ind")
            end, joined = i, st
            while not _one_statement(joined) and end + 1 < len(lines):
                nxt = _next_code(lines, end + 1)
                if nxt is None or nxt - end > 1:
                    break
                end = nxt
                joined = joined + " " + strip_comment(lines[end]).strip()
                if joined.count(";") > 1:
                    break
            if _one_statement(joined):
                if m.group("lead"):
                    out.append(f"{ind}end{nl}")
                out.append(f"{ind}else begin{_tail(sep, cmt)}{nl}")
                out.append(f"{ind}    {st}{nl}")
                # Shifted by as much as the first line moved, so a wrapped
                # argument list stays lined up under its opening parenthesis.
                shift = len(ind) + 4 - m.start("st")
                for t in range(i + 1, end + 1):
                    s = lines[t]
                    if shift >= 0:
                        out.append(" " * shift + s)
                    else:
                        cur = len(s) - len(s.lstrip())
                        out.append(s[min(-shift, cur) :])
                out.append(f"{ind}end{nl}")
                skip.update(range(i + 1, end + 1))
                n += 1
                continue

        out.append(raw)
    return "".join(out), n


ELSE_ALONE = re.compile(r"^(?P<ind>[ \t]*)(?:end[ \t]+)?else[ \t]*$")


def rewrite_else_next(text):
    """F4: `else` alone on its line with a single-statement body below.

    `end` closes on its own line, so nothing merges upward. An `else if` chain
    is left alone -- the body test refuses anything opening a block, and an
    `if` on the next line is exactly that.
    """
    lines = text.splitlines(True)
    out, n, skip = [], 0, set()
    for i, raw in enumerate(lines):
        if i in skip:
            continue
        code = strip_comment(raw)
        m = ELSE_ALONE.match(code)
        if not m:
            out.append(raw)
            continue
        j = _next_code(lines, i + 1)
        if j is None:
            out.append(raw)
            continue
        end = j
        joined = strip_comment(lines[j]).strip()
        while not _one_statement(joined) and end + 1 < len(lines):
            nxt = _next_code(lines, end + 1)
            if nxt is None or nxt - end > 1:
                break
            end = nxt
            joined = joined + " " + strip_comment(lines[end]).strip()
            if joined.count(";") > 1:
                break
        if not _one_statement(joined):
            out.append(raw)
            continue
        ind = m.group("ind")
        nl = "\n" if raw.endswith("\n") else ""
        out.append(f"{code.rstrip()} begin{nl}")
        for t in range(i + 1, j):
            out.append(lines[t])
        body_ind = len(lines[j]) - len(lines[j].lstrip())
        shift = len(ind) + 4 - body_ind
        for t in range(j, end + 1):
            s = lines[t]
            if shift >= 0:
                out.append(" " * shift + s)
            else:
                cur = len(s) - len(s.lstrip())
                out.append(s[min(-shift, cur) :])
        out.append(f"{ind}end{nl}")
        skip.update(range(i + 1, end + 1))
        n += 1
    return "".join(out), n


IF_ELSE_LINE = re.compile(r"^(?P<ind>[ \t]*)(?P<lead>(?:end[ \t]+)?)if[ \t]*\(")


def rewrite_if_else_line(text):
    """F4: `if (C) A; else B;` written on ONE line.

    `rewrite_blocks` refuses any header line carrying an `else`, and that
    refusal is right for `if (a) if (b) x; else y;` -- there the `else` binds to
    the INNER `if` and wrapping the outer body moves it. The test that makes
    this safe is the one `_one_statement` already applies: it rejects a body
    that opens a block or is itself an if/for/case, so a body that passes it
    cannot own the `else`, and the `else` is unambiguously this `if`'s.

    Both arms must be one simple statement. Anything else is left for a human.
    """
    out, n = [], 0
    for raw in text.splitlines(True):
        nl = "\n" if raw.endswith("\n") else ""
        line = raw[: len(raw) - len(nl)]
        code, sep, cmt = line.partition("//")

        m = IF_ELSE_LINE.match(code)
        if not m:
            out.append(raw)
            continue
        close = close_paren(code, code.index("(", m.end() - 1))
        if close is None:
            out.append(raw)
            continue
        head, rest = code[:close], code[close:]
        # ` A; else B;` -- split at the `else` that follows the first `;`.
        sc = rest.find(";")
        if sc < 0:
            out.append(raw)
            continue
        a, tail = rest[: sc + 1].strip(), rest[sc + 1 :].strip()
        if not tail.startswith("else "):
            out.append(raw)
            continue
        b = tail[5:].strip()
        if not _one_statement(a) or not _one_statement(b):
            out.append(raw)
            continue
        ind = m.group("ind")
        out.append(f"{head.rstrip()} begin{_tail(sep, cmt)}{nl}")
        out.append(f"{ind}    {a}{nl}")
        out.append(f"{ind}end{nl}")
        out.append(f"{ind}else begin{nl}")
        out.append(f"{ind}    {b}{nl}")
        out.append(f"{ind}end{nl}")
        n += 1
    return "".join(out), n


def rewrite_cases(text):
    """F5: shift a flat case's body one level right. (new_text, n_shifted).

    The whole span between `case` and its matching `endcase` moves together, so
    relative indentation inside is preserved and nested cases come along at the
    right depth. Only fires when the first item sits at or left of the `case`
    itself, which is what the check counts.
    """
    lines = text.splitlines(True)
    out = list(lines)
    n = 0
    for i, raw in enumerate(lines):
        m = CASE_OPEN.match(strip_comment(raw))
        if not m:
            continue
        base = len(m.group("indent"))
        # The matching endcase, tracking nested case statements.
        depth, end = 0, None
        for j in range(i, len(lines)):
            code = strip_comment(lines[j])
            if CASE_OPEN.match(code):
                depth += 1
            if ENDCASE.match(code):
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end is None:
            continue
        body = [k for k in range(i + 1, end) if lines[k].strip()]
        if not body:
            continue
        first = lines[body[0]]
        if len(first) - len(first.lstrip()) > base:
            continue
        for k in range(i + 1, end):
            if out[k].strip():
                out[k] = "    " + out[k]
        n += 1
    return "".join(out), n


def rewrite_decls(text):
    """F3: no declaration runs over a continuation line. (new_text, n_repacked)."""
    out, last, n = [], 0, 0
    for m in DECL.finditer(text):
        if "\n" not in m.group(0):
            continue
        parsed = decl_type_and_names(m.group("rest"))
        if parsed is None:
            continue
        pre, items = parsed
        indent, kw = m.group("indent"), m.group("kw")
        lead = f"{indent}{kw} {pre} " if pre else f"{indent}{kw} "
        out.append(text[last : m.start()])
        out.append("\n".join(pack(lead, items)))
        last = m.end()
        n += 1
    out.append(text[last:])
    return "".join(out), n


def decl_lines(text):
    """F3: 1-based line numbers of the declarations `rewrite_decls` would
    re-pack. Same predicate, so the listing and the count cannot disagree."""
    hits = []
    for m in DECL.finditer(text):
        if "\n" not in m.group(0):
            continue
        if decl_type_and_names(m.group("rest")) is None:
            continue
        hits.append(text.count("\n", 0, m.start()) + 1)
    return hits


RULES = ("F3", "F4", "F4a", "F5", "F6", "F7")

LABELS = {
    "F3": "declaration over a continuation line",
    "F4": "if/else/for without begin/end      ",
    "F4a": "always body without begin/end     ",
    "F5": "case items not indented            ",
    "F6": "line ending in an operator         ",
    "F7": "unnamed generate block             ",
}


def locate(text):
    """Every finding as {rule: [line, ...]}. `count` is its lengths."""
    return {
        "F3": decl_lines(text),
        "F4": sorted(next_line_bodies(text) + same_line_bodies(text)),
        "F4a": bare_always(text),
        "F5": flat_case_items(text),
        "F6": trailing_operators(text),
        "F7": unnamed_generate_blocks(text),
    }


def count(text):
    return {k: len(v) for k, v in locate(text).items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*", default=list(DEFAULT_DIRS))
    ap.add_argument("--fix-decls", action="store_true", help="apply F3")
    ap.add_argument(
        "--fix-blocks", action="store_true", help="apply F4/F4a, same-line only"
    )
    ap.add_argument("--fix-case", action="store_true", help="apply F5")
    ap.add_argument("--show", action="store_true", help="per-file rows, worst first")
    ap.add_argument(
        "--lines", action="store_true", help="every finding as rule, line and source"
    )
    ap.add_argument("--rule", action="append", help="restrict --lines to a rule")
    args = ap.parse_args()

    files = []
    for p in args.paths:
        q = ROOT / p
        files += sorted(q.rglob("*.v")) if q.is_dir() else [q]

    want = set(args.rule) if args.rule else set(RULES)

    tot = dict.fromkeys(RULES, 0)
    rows, touched = [], 0
    for f in files:
        text = f.read_text(encoding="utf-8")
        at = locate(text)
        n = {k: len(v) for k, v in at.items()}
        for k in RULES:
            tot[k] += n[k]
        if any(n.values()):
            rows.append((sum(n.values()), _rel(f), n))
        if args.lines:
            found = sorted(
                (ln, k) for k in RULES if k in want for ln in dict.fromkeys(at[k])
            )
            if found:
                src = text.splitlines()
                print(f"{_rel(f)}")
                for ln, k in found:
                    body = src[ln - 1].rstrip() if ln <= len(src) else ""
                    print(f"  {k:<4}{ln:>5}: {body}")
                print()
        # Chained (a later --fix used to overwrite an earlier one's output) and
        # run to a fixpoint: one pass reaches one nesting level, so wrapping
        # `for (i..) for (j..) x;` leaves the inner `for` for the next pass.
        new = text
        for _ in range(8):
            was = new
            if args.fix_decls:
                new = rewrite_decls(new)[0]
            if args.fix_blocks:
                new = rewrite_blocks(new)[0]
                new = rewrite_blocks_next(new)[0]
                new = rewrite_tails(new)[0]
                new = rewrite_if_else_line(new)[0]
                new = rewrite_else_next(new)[0]
            if args.fix_case:
                new = rewrite_cases(new)[0]
            if new == was:
                break
        if new != text:
            f.write_text(new, encoding="utf-8")
            touched += 1

    rows.sort(reverse=True)
    for _, rel, n in rows if args.show else []:
        print(f"{rel}: " + " ".join(f"{k}={n[k]}" for k in RULES if n[k]))

    print()
    for k in RULES:
        print(f"{k} {LABELS[k]}: {tot[k]}")
    print(f"\n{'total':<39}: {sum(tot.values())}   over {len(files)} file(s)")
    fixing = args.fix_decls or args.fix_blocks or args.fix_case
    if fixing:
        print(f"rewrote {touched} file(s)")
    # Non-zero on a finding, so check.py can gate on it. A --fix run reports
    # the count it read BEFORE writing, so failing there would be a lie.
    return 1 if (sum(tot.values()) and not fixing) else 0


if __name__ == "__main__":
    sys.exit(main())
