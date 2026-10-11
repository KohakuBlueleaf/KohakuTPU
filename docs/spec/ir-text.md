# IR text: one statement syntax, a module per kernel

Every IR level has a text, and no level's text is Python. The levels share one
statement grammar; what a statement means is its level's reader's, which a
project writes for its own words (its unit ops, layouts, L3 ops).

Code: `software/language/kohakutpu/language/text/` (the grammar `ktpu.lark`,
`syntax.py`, `reader.py`, `writer.py`, `meta.py`, `module.py`). The level
readers: `language/l3/reader.py`, `language/l2/reader.py`, and the L1 body's
in the compiler (`software/compiler/kohakutpu/compiler/emit/`).

## 1. Statements

```
[target =] op args [: annotation]
    block statements, indented
```

- **One statement a line.** No statement spans lines, except that a newline
  inside `( )` or `[ ]` is not a statement end.
- A block is the statements indented under a statement (4 columns a level).
- `#` starts a comment to the end of the line.
- Arguments are apart by spaces or by commas; a statement prints back with the
  separator it was read with. `name=value` is a keyword argument; every other
  argument is positional.

| term | text | parses to |
|---|---|---|
| name | `x`, `reduce.max` (a dot followed by a letter) | `Name` |
| integer | `42`, `1_000`, `0x8010_0000`, `16K` (K/M/G) | `Int(value, style)` |
| float | `1.5`, `1e-05` | `Float` |
| dims | `64x64x2` | `Dims` |
| string | `"kohakutpu-l1"` (no quote, no newline inside) | `Str` |
| negation, not | `-x`, `!p` | `Neg`, `Not` |
| offset | `buf+0x40` | `Offset` |
| arithmetic | `c*64K + t*2K`, `(i%2)*4 + j`, `n/2 - 1` | `BinOp` (`-` `*` `/` `%`; `+` is `Offset`) |
| comparison | `i < 7`, `k == 0` | `Compare` |
| computed name | `v{16 + j}`, `e{s*4 + u}`, `v{4*q}[2:]` (name touching `{`) | `Computed`, folded to a `Name` or `View` when read |
| span | `0..H` | `Span` |
| call | `tiles(L, bq)`, `f(x: f16[N], n=2)` (name touching `(`) | `Call` of terms, `Typed`, `Kw` |
| view | `q[h, i, :]`, `m[:, *]`, `x[lo : hi]` (name touching `[`) | `View` of terms, `Slice`, `NewAxis` |
| tuple | `()`, `(x,)`, `(1, 0)` | `Tuple` |
| assignment | `n=128`, `0x10 = 1`, `o[h] = x` | `Assign` |
| membership | `h in 0..H` | `Member` |
| arrow | `->`, `<-` | `Arrow` |

A name and a bracket apart are two terms: `MG (1, 0)` is a name and a tuple.
Binary operators bind tighter than argument separation, so `a -b` is a
subtraction; a negative argument after another is written after a comma. A
binding's target may be computed: `q{i*4 + u} = mark vc0`.

**Canonical text.** The printer writes one form of each term: hexadecimal in
groups of four digits, sizes in K/M/G when whole, a one-binding statement as
`op name = value`, keyword arguments after positional ones, annotations of one
block aligned two columns past the longest statement (at most column 48), and a
blank line around each top-level block. Reading canonical text and printing it
gives the same text.

**Diagnostics.** A parse error and a meaning error alike are a `TextError`
naming the file, the line and the column, with the line and a caret.

## 2. Modules

A module is one text holding a kernel at every level, the levels' bodies side
by side (`module.py`). Its top-level statements:

```
target ktpu.v9                       # the machine the L1 bodies address
fn NAME.LEVEL(p: TYPE, ...) [-> TYPE]
    body                             # LEVEL is l3, l2 or l1
image NAME(params)
    statements                       # a unit's program, its params constants
macro NAME(params)
    statements                       # text expanded where `expand NAME(args)` stands
```

- One name carries at most one body a level, and the bodies under one name are
  one kernel written at several levels: a compiler's output from the higher
  body is gated against the hand-written lower one.
- A body stays statements in the module; its level's reader gives it meaning.
  A project's reader for each level is its own.
- `image` and `macro` names share one table; a name defined twice is refused.

The L3 body's text is [l3-program.md](l3-program.md) §2. KohakuTPU's modules
(suffix `.ktpu`), their L1 words and kernels:
[../projects/kohakutpu/ir/ktpu.md](../projects/kohakutpu/ir/ktpu.md).

**Read-time expansion** (`meta.py`). Before a level reader sees a body, the
expander rewrites it against an environment of constants (a macro's or image's
arguments, a fn's bound parameters, loop variables):

| construct | effect |
|---|---|
| `for v in LO..HI unroll` (block) | the block once per value of `v`; an L1 reader unrolls every `for` |
| `when COND` (block) | the block if the constant condition holds, else nothing |
| `expand NAME(args)` | the macro's statements with its parameters bound |
| arithmetic, comparisons | folded when both sides are constants; a free name plus constants stays one `Offset` |
| computed names | `v{i + 1}` to `v3`; a computed binding target likewise |

A name the environment binds to a number is that number; a macro argument that
is a name stays a name, and a statement whose op is such a parameter takes the
name as its op. A name on the left of `=` or `+=` (a keyword, a register) is
never substituted. The expanded body holds no meta statement.

## 3. Toolchain

The grammar is LALR(1) with a contextual lexer (lark), the indentation a
post-lexer. `ktpu.lark` is the grammar's one definition: a port to another
parser (pest behind pyo3 and wasm; tree-sitter for editors) is written from it
and is conformant when it reads the kernels of
`software/language/kohakutpu/language/kernels/` to the same statements.

## 4. Gates

`software/language/tests/test_syntax.py` (the grammar, canonical text, each
kind of wrong text refused at its line), `test_l3.py` and `test_l2.py` (the
level readers), `software/compiler/tests/test_emit_image.py` and
`test_emit_node.py` (the L1 bodies).
